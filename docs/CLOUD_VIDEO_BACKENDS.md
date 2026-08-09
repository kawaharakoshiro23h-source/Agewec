# クラウド動画生成バックエンド導入方針

確認日: 2026-08-02

## 結論

現在のLangGraph全体構造は変えずに、Phase 06の動画生成をComfyUIから
クラウドAPIへ差し替えられる。

ただし現在の`ProductionRequest`はLTX/ComfyUI固有の
`frames / fps / steps`を持つため、API URLを置き換えるだけでは動かない。
以下の2点を局所的に変更する必要がある。

1. Phase 05.5で共通の生成依頼を作り、モデル固有の尺・解像度へ変換する
2. Phase 06でプロバイダー別Adapterを呼び、ジョブ送信・待機・MP4取得を行う

Phase 07以降の技術QA、差し戻し、FFmpeg結合、Provenanceは基本的に再利用できる。

## API提供状況

| 候補 | 公式に確認できたAPI機能 | 主な差異 |
|---|---|---|
| Veo 3.1 Fast | Gemini APIでimage-to-video、音声付き動画、最初/最後のフレーム、参照画像 | 基本8秒単位。長時間処理をポーリングする |
| Seedance 2.0 | Runway APIでtext/image/video-to-video、4〜15秒、参照画像・キーフレーム・音声 | Runway経由で利用可能。モデル別に許容解像度が異なる |
| LTX-2.3 Pro | LTX APIでimage-to-video、最大20秒、最大4K、同期/非同期、同期音声 | 直接APIあり。Comfy版とはパラメータ名・モデル構成が異なる |
| Runway Gen-4.5 | Runway APIでtext/image-to-video、2〜10秒、seed指定 | タスク送信後に完了待ち・URL取得が必要 |
| Luma Ray 3.2 | Luma APIでtext/image/video、5秒/10秒、複数キーフレーム、1080p | ジョブAPI。独自のファイルアップロードと生成Optionsを使う |

Seedance 2.0、Gen-4.5に加え、Veo 3.1 FastもRunway APIの同じ
image-to-videoエンドポイントから選択できる。最初の実装を小さくするなら、
Runway Adapter 1つでこの3モデルを切り替える方法が有力。

## 現在の処理と変更範囲

```text
Director
  ↓ 画像・Prompt・演出
Support Video Creator（Phase 05.5）
  ↓ 共通VideoGenerationRequest
Video Backend Adapter（Phase 06）
  ├─ ComfyUI
  ├─ Runway（Gen-4.5 / Seedance 2 / Veo 3.1 Fast）
  ├─ LTX API（LTX-2.3 Pro）
  └─ Luma API（Ray 3.2）
  ↓ ローカルに保存したMP4 + 生成メタデータ
Cut QA → Review Gate → FFmpeg → Provenance
```

変更対象は主に`support_video_creator`と`image_video_production`。
Storyboard、Asset Curator、Director、Review Gate、Post Productionの役割は維持する。

## 共通RequestとAdapter

共通Requestはモデル固有の値を直接要求せず、意図を表す。

```yaml
cut_id: 3
backend: runway
model: seedance2
image_path: /local/source.jpg
positive_prompt: "..."
negative_prompt: "..."
requested_seconds: 6
aspect_ratio: "16:9"
target_resolution: "1080p"
seed: 12345
audio_policy: discard | preserve | required
```

Adapterの共通契約:

```text
preflight() -> 接続・Model利用可否
capabilities() -> 許容尺・解像度・seed・音声など
generate(request) -> output_path, provider, model, job_id,
                     elapsed_seconds, duration, cost, native_audio
```

Adapter内部が次を吸収する。

- ローカル画像をbase64、署名付きUpload、公開URLのいずれかへ変換
- 要求秒数をモデルの許容尺へ切り上げ、Phase 08で余剰をtrim
- 非同期Jobの送信、ポーリング、失敗・429・timeout処理
- 生成MP4をローカルへDownload
- seed非対応Modelでは「指定不能」を明示
- APIキー、Job ID、Model、費用、利用秒数を証跡へ記録

## 想定される注意点

### 尺

各社の許容尺が違う。Storyboardの6秒を、Veoでは8秒、Lumaでは10秒などへ
変換する可能性がある。短い動画を無理に延ばさず、許容尺以上で生成して
Phase 08でtrimする方が安全。

### 解像度・縦横比

`width / height`を自由指定できないAPIがある。共通Requestでは解像度Tierと
Aspect Ratioを指定し、Adapterが各社の列挙値へ変換する。

### seed

Runwayはseedを受け取るが、全Model・全APIで同じ再現性は保証できない。
Provenanceでは「要求seed」と「Providerが受理したseed」を分けて記録する。

### 音声

Veo、Seedance、LTXなどは音声付き出力を返せる。現在のPhase 08で
無条件に音声を捨てると利点を失うため、`audio_policy`を先に決める。
BGMを後付けする場合は`discard`、Model音声を使う場合は`preserve`にする。

### Prompt

各社でPrompt長、negative prompt、カメラ指示の解釈が異なる。
Director出力は共通のまま保ち、Adapter直前にModel別の軽い変換を行う。

### 費用と失敗

クラウド生成は再試行ごとに課金され得る。LLMとは別に動画生成用の
`max_cost_per_run`、カット別再試行上限、手動承認を設ける。
自動Fallbackで別Providerを呼ぶ場合も、課金前にPolicy確認を挟む。

### Moderation・保存期間

各社のContent Moderationと一時Download URLに対応する。
完了後はすぐにMP4をrun別ローカルフォルダへ保存し、URLだけを成果物にしない。

## 推奨導入順

1. 共通Adapter Interfaceを定義し、既存Comfy/mockを同じ契約で包む
2. 既存の1カットテストで、挙動が変わらないことを確認
3. Runway Adapterを追加し、Gen-4.5を1カットだけ試す
4. 同じAdapterでSeedance 2 / Veo 3.1 Fastを切り替える
5. 必要ならLTX Direct Adapter、Luma Adapterを追加
6. 採用Modelを決めてから、全カット本番生成を行う

最初から5種類すべてを本番対応する必要はない。1つを実装・比較し、
画質と費用が不足した場合だけ次を追加する方が安全。

## 公式資料

- [Google: Veo 3.1 in Gemini API](https://ai.google.dev/gemini-api/docs/veo)
- [Google: Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Runway API Reference](https://docs.dev.runwayml.com/api/)
- [Runway: Available Models](https://docs.dev.runwayml.com/guides/models/)
- [Runway: API Input Parameters](https://docs.dev.runwayml.com/assets/inputs/)
- [LTX API Documentation](https://docs.ltx.video/)
- [LTX API Pricing](https://docs.ltx.video/pricing)
- [Luma Ray 3.2 API](https://lumalabs.ai/api)
- [Luma Generations API](https://docs.agents.lumalabs.ai/api/resources/generations/methods/create)

