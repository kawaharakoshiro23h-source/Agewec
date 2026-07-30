# AGEWEC Agent 自律性・Human-in-the-loop 設計メモ

## 目的

LangGraph の各ノードについて、人間が承認する運用と AI が自動判定する運用を切り替えられるようにする。

ComfyUI は画像・動画・音声生成の実行エンジンとして利用し、全体の進行管理、評価、分岐、再試行、人間承認、ログ保存は LangGraph が担当する。

## 実行モード

プロジェクト開始時に以下のプリセットを選択できるようにする。

1. **Full Review**
   - すべてのノード完了後に人間の承認を要求する。
2. **Full Auto**
   - すべてのノードを、事前生成した評価基準に従って AI が判定する。
3. **Custom**
   - ノードごとに `human` / `auto` を選択する。

`auto` の場合でも、評価不能、信頼度不足、再試行上限到達、重大な権利・品質問題がある場合は人間へエスカレーションする。

## 起動時のポリシー生成

プロジェクトの概要、目的、制約、提出要件を受け取った `Policy Planner` が、各ノードの実行ポリシーを構造化 JSON として生成する。

自由なプログラムコードを生成させるのではなく、固定スキーマの値だけを LLM に埋めさせる。

各ノードには次の3種類の条件を持たせる。

- **Entry conditions**: ノードを開始してよい条件
- **Acceptance criteria**: 出力を合格とする条件
- **Routing conditions**: 不合格時の戻り先、再試行、エスカレーション条件

### ポリシー例

```json
{
  "node": "visual_qa",
  "mode": "auto",
  "entry_conditions": [
    "動画ファイルが存在する",
    "モデル名・seed・プロンプトが記録されている"
  ],
  "acceptance_criteria": [
    {
      "type": "deterministic",
      "criterion": "動画が指定尺以内で再生可能"
    },
    {
      "type": "vlm",
      "criterion": "建物や被写体に重大な変形がない",
      "minimum_score": 4
    },
    {
      "type": "vlm",
      "criterion": "北九州の観光素材としての整合性",
      "minimum_score": 4
    }
  ],
  "max_retries": 2,
  "on_failure": "image_video_production",
  "on_retry_exhausted": "human_review"
}
```

## ノード完了後の共通フロー

```text
ノード処理完了
  ↓
実行ポリシーを確認
  ├─ human
  │    └─ 人間が「承認 / 修正指示 / 再実行 / スキップ」を選択
  └─ auto
       └─ AI Evaluator が合否判定
            ├─ 合格 → 次のノードへ
            ├─ 不合格 → 修正指示を生成して再実行
            └─ 再試行上限 → 人間へエスカレーション
```

人間から入力された修正指示は LangGraph State に保存し、対象ノードの次回プロンプトへ追加する。

## 評価方法

判定は可能な限り決定的な検査と AI 評価を分離する。

### プログラムで検査する項目

- ファイルの存在
- 動画・音声が正常に開けるか
- 尺、解像度、FPS、ファイル形式
- 必要なモデル名、seed、プロンプト、出典の記録
- 素材ライセンス確認フラグ

### LLM / VLM で評価する項目

- 物語の一貫性
- 北九州の観光訴求力
- コンセプトとの整合性
- 映像の破綻、ちらつき、被写体の変形
- ナレーションと映像の意味的な一致

## 画面表示

AGEWEC 専用ダッシュボードを用意し、ComfyUI とは分離する。

ダッシュボードには以下を表示する。

- LangGraph 全体のノード構成
- 現在実行中のノード
- `待機 / 実行中 / 承認待ち / 完了 / 失敗 / 再試行` の状態
- 各ノードの入力と出力
- 生成画像、動画、音声のプレビュー
- LLM/VLM の構造化された評価結果、点数、採用・却下理由
- 使用プロンプト、モデル、seed、処理時間
- `承認 / 修正して再実行 / スキップ` ボタン
- 人間が修正指示を入力するテキスト欄

非公開の内部思考過程ではなく、意思決定のために生成した構造化結果と根拠を表示する。

## LangGraph と ComfyUI の分担

```text
AGEWEC Dashboard
  ↓ 状態表示・承認・修正指示
LangGraph
  ├─ Policy Planner
  ├─ Planner / Writer / Director
  ├─ Asset Curator
  ├─ QA / Review / Retry
  ├─ Human-in-the-loop
  ├─ Assembly / Provenance
  └─ ComfyUI API を呼び出す
       ├─ Image
       ├─ Image-to-Video
       └─ ACE-Step 等の Audio
```

ComfyUI のノードグラフはメディア生成処理の固定テンプレートとする。LangGraph は API 用ワークフロー JSON の入力画像、プロンプト、seed、解像度などを差し替えて実行する。

LangGraph のグラフ自体を ComfyUI に表示するのではない。

## 保存・再現性

以下を実行単位で保存する。

- プロジェクト開始時に生成したポリシー JSON
- ポリシーのバージョン
- 各ノードの入出力
- 人間の承認・修正指示
- AI Evaluator の構造化判定
- 再試行回数と分岐履歴
- ComfyUI ワークフローのバージョン
- 使用モデル、seed、プロンプト
- 最終成果物と素材の出典

実行中に評価基準を無断で再生成せず、変更時は新しいバージョンとして記録する。

## 最小実装方針

締切優先の最小構成では、各ノード専用の承認画面を個別実装せず、共通の `Review Gate` を再利用する。

1. LangGraph のノード状態をストリーミング表示
2. 共通 Review Gate で `human` / `auto` を判定
3. 人間の承認・修正指示による中断と再開
4. Auto Evaluator による合否判定と最大1〜2回の再試行
5. ComfyUI はまず Video Production ノードのみ接続
6. 実行ログと Provenance を JSON で保存

## ローカル動画生成ベンチマーク

### 2026-07-29: LTX-Video 2B v0.9.5

- マシン: MacBook Pro M5 / 32 GB ユニファイドメモリ
- 実行環境: ComfyUI Desktop / Apple Silicon MPS
- モデル: `ltx-video-2b-v0.9.5.safetensors`（非 Distilled）
- 解像度: 768 × 512
- フレーム数: 97
- サンプリングステップ: 30
- 出力 FPS: 24
- 動画尺: 約4秒
- 実測処理時間: **9分56秒**

今後、軽量設定や Distilled モデルの実測値を同じ形式で追加し、速度と品質を比較する。

### 2026-07-29: LTX-Video 2B v0.9.5 軽量設定

- マシン: MacBook Pro M5 / 32 GB ユニファイドメモリ
- 実行環境: ComfyUI Desktop / Apple Silicon MPS
- モデル: `ltx-video-2b-v0.9.5.safetensors`（非 Distilled）
- 解像度: 576 × 384
- フレーム数: 49
- サンプリングステップ: 20
- 出力 FPS: 24
- 動画尺: 約2秒
- 実測処理時間: **2分38秒**
- 元設定比: 約3.8倍高速、処理時間を約74%短縮
