# AGEWEC 計算資源・生成バックエンド引き継ぎメモ

## この文書の位置づけ

AGEWEC向け映像生成ワークフローについて、これまでに確認できたこと、計算資源の選択方針、将来大学のGPUサーバーを利用する場合の引き継ぎ事項をまとめる。

現時点では計算資源の追加調査よりも、LangGraphを中心としたワークフロー設計と最小実装を優先する。

大学GPUへのVPN接続は利用できる可能性があるが、現在の作業範囲から外す。ワークフローの最小実装が完成し、GPU利用の許可と接続条件が確認できた場合にのみ再開する。

認証情報、VPN設定、APIキー、SSH秘密鍵は、このリポジトリへ保存しない。

## プロジェクトの基本方針

全体制御はLangGraph、メディア生成はComfyUI、ローカルLLM候補はLM Studio、最終的な動画結合はFFmpegが担当する。

```text
AGEWEC Dashboard
  ↓ 状態表示・承認・修正指示
LangGraph
  ├─ 企画・コンセプト
  ├─ Writer / Storyboard
  ├─ Asset Curator
  ├─ Director / Prompt生成
  ├─ Human / Auto Review Gate
  ├─ Visual QA
  ├─ Post Production
  └─ Provenance
       ↓
生成バックエンド
  ├─ ComfyUI Local
  ├─ Comfy Cloud
  ├─ RunPod等の従量課金GPU
  └─ 将来: 大学GPUサーバー
```

ComfyUI内部のノードグラフをLangGraph上に再実装する必要はない。LangGraphから見たComfyUIは、画像、プロンプト、Seed、解像度などを受け取って成果物を返す一つの生成ジョブとする。

## 現在までに実証できたこと

MacBook Pro M5、32GBユニファイドメモリ上のComfyUI Desktopで、画像とPositive Promptから短い動画をローカル生成できた。

現在の手動PoCは以下の処理で構成される。

```text
入力画像
  ↓
Positive / Negative Prompt
  ↓
LTX Image-to-Video
  ↓
Custom Sampler
  ↓
VAE Decode
  ↓
Create Video
  ↓
Save Video
```

北九州の夜景画像に対して狐と氷の動画が生成された事例は、テンプレートに残っていた狐のPositive Promptが原因だった。モデル精度だけの問題ではない。Image-to-Videoでは、元画像の保持、動かす対象、カメラ動作を明示する必要がある。

### 実測ベンチマーク

#### 元設定

- モデル: LTX-Video 2B v0.9.5、非Distilled
- 解像度: 768 × 512
- フレーム数: 97
- ステップ数: 30
- FPS: 24
- 動画尺: 約4秒
- 実測時間: 9分56秒

#### 軽量設定

- モデル: LTX-Video 2B v0.9.5、非Distilled
- 解像度: 576 × 384
- フレーム数: 49
- ステップ数: 20
- FPS: 24
- 動画尺: 約2秒
- 実測時間: 2分38秒
- 元設定比: 約3.8倍高速、処理時間を約74%短縮

軽量設定をDraft生成の基準にし、本番採用カットだけ解像度、フレーム数、品質設定を上げる。

## 計算資源の選択肢

### 1. ローカルMac

用途:

- ワークフロー開発
- API連携の確認
- 短い低解像度Draft
- オフライン実行
- クラウド障害時のフォールバック

長所:

- 追加料金なし
- データを外部へ送らない
- ローカルで実行可能な構成を示せる

短所:

- 動画生成が遅い
- LM StudioとComfyUIが32GBのユニファイドメモリを共有する
- 長尺、高解像度、複数カットの反復には不向き

### 2. LM StudioとComfyUIの直列実行

ローカルLLMを利用する場合、LM StudioとLTXを同時常駐させず、次の順番で実行する。

```text
LM Studioで企画・全カットのPromptをまとめて生成
  ↓
結果をLangGraph State / JSONへ保存
  ↓
LM Studioモデルをアンロード
  ↓
ComfyUIで動画生成を連続実行
```

1カットごとにLLMと動画モデルを交互にロードすると、モデル読み込み時間が増える。企画フェーズと生成フェーズをまとめる。

### 3. 有料LLM API

企画、絵コンテ、Prompt生成、Review、Visual QAを有料LLM APIへ任せ、ローカルメモリをComfyUIへ集中させる構成は有力。

```text
クラウドLLM API
  ├─ Executive Producer
  ├─ Creative Director
  ├─ Writer / Storyboard
  ├─ Director / Prompt生成
  └─ Review / QA

ローカル
  ├─ LangGraph
  ├─ ComfyUI
  ├─ FFmpeg
  └─ Dashboard / Logs
```

特定プロバイダーやモデル名に固定せず、`LLMProvider`インターフェースでLM Studioと有料APIを切り替えられる設計にする。

### 4. Comfy Cloud

ComfyUI公式クラウド。ローカルComfyUIに近いAPI形式で実行できるため、移行しやすい。

一方で、クラウドGPU/API利用は月額プランを前提とし、完全な月額なし従量課金ではない。使用中のモデル、カスタムノード、ワークフローがクラウド側で利用可能か事前確認が必要。

### 5. RunPod等の従量課金GPU

RunPodは、ComfyUIを載せたGPU Podを起動し、実際に起動したGPU時間に応じて支払う方式。現在のComfyUIワークフローとモデルを比較的そのまま持ち込みやすい。

想定手順:

```text
GPU Pod起動
  ↓
ComfyUIテンプレート起動
  ↓
モデル・ワークフロー配置
  ↓
生成
  ↓
成果物ダウンロード
  ↓
Pod停止・削除
```

注意点:

- モデルダウンロードと初期設定中も、Pod起動中ならGPU料金が発生する
- 永続ストレージを残す場合は、停止中もストレージ料金が発生し得る
- 生成完了後の停止・削除を忘れない
- 初回セットアップ時間を締切直前に見込まない

RunPod以外にも、RunComfy、Vast.ai、Replicate、fal.ai等が候補になる。現在のComfyUIグラフを保つならGPUレンタル、簡単なAPI呼び出しを優先するならモデルAPIを選ぶ。

## 推奨するHybrid実行プロファイル

```text
Local Draft
  ├─ 低解像度
  ├─ 短いフレーム数
  ├─ 採用可否の確認
  └─ 無料

Cloud Final
  ├─ 承認済みカットのみ
  ├─ 高解像度・高品質
  ├─ 再試行上限あり
  └─ 使用コストを記録
```

設定例:

```yaml
execution_profile: hybrid
llm_backend: openai
draft_media_backend: local
final_media_backend: cloud
fallback_media_backend: local
```

バックエンド名は実装時に固定せず、次のような共通インターフェースを持たせる。

```text
generate_video(spec, backend)
```

`spec`には以下を含める。

- 入力画像
- Positive / Negative Prompt
- モデル・ワークフローバージョン
- Seed
- 解像度
- フレーム数
- ステップ数
- FPS
- 出力先
- 最大実行時間
- 最大コスト

## Budget Gate

自動モードがクラウド生成を無制限に繰り返さないよう、本番生成の前にBudget Gateを置く。

```text
Final生成要求
  ↓
Budget Gate
  ├─ 予算内 → 実行
  ├─ 予算超過 → ローカルへフォールバック
  └─ 再試行・高額処理 → 人間承認
```

最低限保存する項目:

- プロジェクト総予算
- 1カットの最大予算
- クラウド再試行上限
- 予測コスト
- 実コスト
- 使用バックエンド
- GPU種別
- 実行時間
- 失敗時の課金有無

## 将来、大学GPUへ引き継ぐ場合

大学GPUは有力なバックエンド候補だが、現在は調査を停止する。再開条件は次の通り。

1. LangGraphからローカルComfyUIをAPI実行できる
2. Draftから成果物回収までの最小フローが完成している
3. 大学GPU利用の正式な許可が取れている
4. VPN、SSH、GPU利用ルールが確認できている

再開時に管理者へ確認する項目:

- GPU型番とVRAM
- 利用可能時間と共有ルール
- CUDA、PyTorch、Dockerの利用可否
- SSH接続方法
- VPN接続条件
- Slurm等のジョブスケジューラー有無
- モデル保存可能容量
- Hugging Face等への外部通信可否
- コンテスト用途での利用可否
- 生成物の持ち出し・保存ルール

推奨する接続方式:

```text
手元PC
  ↓ VPN
SSH
  ↓
大学GPU上のComfyUI
  ↓
SSHポートフォワード
localhost上のAPIとしてLangGraphから利用
```

ComfyUIのポートをインターネットへ直接公開しない。可能であればDockerまたは管理者指定のジョブ環境で実行する。

LangGraph側では大学GPUを特別扱いせず、接続先の異なるComfyUIバックエンドとして扱う。

```yaml
media_backends:
  local:
    type: comfyui
    base_url: http://127.0.0.1:8188

  university_gpu:
    type: comfyui
    base_url: http://127.0.0.1:8189
```

VPN、SSHトンネル、GPUジョブの起動・停止はLangGraph外部の運用処理とし、最初の実装では自動化しない。

## 現在の優先順位

1. LangGraph Stateとノード入出力の設計
2. ComfyUIワークフローのAPI用JSON化
3. ローカルComfyUI APIアダプター
4. Human / Auto Review Gate
5. 最小Dashboard
6. FFmpeg Assembly
7. Provenanceと実行ログ
8. 有料LLM APIへの切り替え
9. クラウド動画生成バックエンド
10. 必要になった場合のみ大学GPUを再検討

計算資源は交換可能なバックエンドとして設計し、現在の最優先事項であるワークフロー本体の完成を妨げないようにする。
