# AGEWEC Pipeline

## このプロジェクトについて

AGEWEC 2026への応募を目的に開発した、**AIエージェントによるプロモーション映像制作システム**です。テーマ・ターゲット・尺などの条件と、北九州市の公式観光写真を入力すると、複数のAIエージェントが企画、絵コンテ、素材選定、演出、動画生成、品質確認、編集、証跡作成を分担して実行します。

単に動画生成APIを1回呼び出すデモではなく、実際の制作業務にある「役割分担」「工程間の情報受け渡し」「人間による承認と差し戻し」「失敗時の部分再実行」「費用管理」「制作根拠の記録」までを、一つの再開可能なワークフローとして設計した点が特徴です。

このシステムを使って、北九州市の夜景を題材とした**48秒・8カットの観光プロモーション映像**を制作し、完成動画と同時に、各工程の判断・入力・出力を確認できるHTMLレポートと証跡データを生成しました。

## 解決したかった課題

生成AIで映像を作る場合、画像や動画そのものは生成できても、次のような制作工程上の課題が残ります。

- 企画、台本、素材、演出の判断が一つのプロンプトに混ざり、修正理由を追いにくい
- 一部のカットだけを直したい場合でも、全体を作り直して時間とAPI費用を消費する
- AIの出力を人間が確認・承認する場所が曖昧
- 使用素材、生成条件、フィードバック、品質判定が完成動画から分からない
- 長時間処理の中断後に、生成済み成果物を維持したまま再開しにくい

本プロジェクトでは、制作工程を状態機械として分割し、各工程の入出力と判断を保存することで、これらの問題に対応しています。

## システムの流れ

1. **制作条件を入力** — 応募賞、テーマ、視聴者、目標尺などを設定
2. **企画・構成を設計** — Executive Producer、Creative Director、Writerが制作方針と絵コンテを作成
3. **素材を選定** — ローカルにカタログ化した公式観光写真から、各カットに適した素材と代替候補を選択
4. **演出と生成方式を決定** — Directorがカメラワーク、生成指示、Image-to-Video / Text-to-Video、動画モデルをカット単位で設計
5. **動画を生成** — Runway API、ComfyUIまたはmockバックエンドを共通インターフェースから実行
6. **品質を確認** — 技術QAと人間のReview Gateで、承認または対象工程への差し戻しを判断
7. **動画を結合・仕上げ** — FFmpegでカットを正規化し、一本の動画へ結合
8. **提出資料を出力** — 完成動画、工程レポート、使用素材、判断履歴、費用・時間、ハッシュ付き証跡を生成

## 技術的な特徴

- **LangGraphによる状態管理** — 分岐・差し戻し・部分再実行を含む制作フローをグラフとして実装
- **Human-in-the-loop** — 各工程で人間が成果物を確認し、承認・自然言語フィードバック・中止を選択可能
- **構造化されたAI出力** — Pydanticで役割ごとの出力形式と工程間の契約を検証
- **バックエンドの抽象化** — mock、ComfyUI、Runwayの違いを吸収し、モデル固有の入力条件を課金前に検査
- **安全な再実行** — 対象カットだけを戻し、承認済みカットと生成済み成果物を維持
- **永続化と再開** — SQLite checkpointに状態を保存し、終了・例外・中断後も同じrun IDから再開
- **費用と実行回数の制御** — API費用上限、カット別試行回数、全体実行時間などのガードを実装
- **トレーサビリティ** — AIと人間の判断、プロンプト、素材出典、生成条件、QA、ハッシュをHTML・JSONで出力
- **回帰テスト** — Phase間の接続、差し戻し、APIリクエスト、レポート、resumeを含む261件の自動テストを整備

主な使用技術は、Python、LangGraph、Pydantic、SQLite、OpenAI互換LLM API、Runway Dev API、ComfyUI、FFmpeg、HTML/JSONです。

## ディレクトリ

```text
configs/             実行設定（local mock / LLM・外部動画API）
src/agewec_v2/       現行パイプライン本体
tests/               自動テスト
docs/                設計・運用資料
  └── submission/    応募フォーム案などの提出関連文書
scripts/             補助スクリプト
workflows/           ComfyUI API workflow
runtime/             新規Run・checkpoint・提出候補（Git追跡外）
deliverables/        確定提出物・派生版（本体はGit追跡外）
archive/legacy_v1/   参照されない旧版
workflow_v2/         過去Run・旧checkpointの読み取り互換領域
```

パッケージ名は互換性のため当面`agewec_v2`を維持します。正式な実装は`src/agewec_v2/`です。

## 本番経路

```text
run.py
  └─ graph_safe.py          Review Gateと実行上限を持つ本番グラフ
       └─ nodes_runtime.py  本番ノードの公開点
            ├─ roles/       Phase 01–05（LLM役割処理）
            ├─ fallbacks/   LLMを使わない決定的処理
            └─ phases/      Phase 05.5–10（生成・QA・編集・証跡）
```

`nodes.py`、`nodes_llm.py`、`pipeline_runtime.py`は旧import向けの互換窓口です。新しい処理は責務に対応する`roles/`、`fallbacks/`、`phases/`へ追加します。

## セットアップ

```bash
cd /path/to/Agewec
uv sync
```

環境変数が必要な場合は`.env.example`を参考に、プロジェクトルートの`.env`へ設定します。秘密情報をGitへ追加しないでください。

## 安全なローカル実行

外部のLLM・動画生成APIを呼ばないmock設定です。

```bash
uv run agewec --config configs/config_local.yaml
```

全工程を自動承認する場合のみ`--auto`を付けます。H3が必須設定なら、`--auto`でも最終確認は停止します。

## LLM・動画バックエンドを使う実行

`configs/config_llm.yaml`の`llm`、`production.backend`、`production.model`、費用上限を確認してから実行します。この設定は外部APIへ課金リクエストを送る可能性があります。

```bash
# 接続確認（動画生成は行わない）
uv run python -m agewec_v2.llm_check
uv run python -m agewec_v2.comfy_check

# 本番実行
uv run agewec --config configs/config_llm.yaml
```

## 中断したRunの再開

```bash
uv run agewec --resume run-xxxxxxxxxx
```

新規checkpointは`runtime/checkpoints/checkpoints.sqlite`へ保存されます。完了済みの旧Runは、旧`workflow_v2/work/checkpoints.sqlite`から読み取り可能です。保存済み設定を使うため、`--resume`と`--config`/`--preset`は同時指定できません。

## 人間確認

既定の`autonomy_preset: manual`では各Review Gateで停止します。Cut QAでは映像を確認し、次の操作を選べます。

```text
[y] 問題を承知で承認して次へ
[d] 演出・生成指示を修正
[s] 元画像を変更
[g] 動画モデル・生成設定を変更
[n] 同じ条件で再生成
[a] 中止
```

人間の明示承認はAIのQA判定より優先されます。承認済みの他カットは、対象カットだけの修正で再生成されません。

## 実行データ

```text
runtime/runs/<run_id>/
├── state.json
├── events.jsonl
├── review.html
├── cuts/
│   └── cut_01/
│       ├── request.json
│       ├── attempt_01.mp4
│       ├── attempt_01_qa.json
│       └── decision.json
└── final/final_video.mp4

runtime/submissions/<run_id>/
├── final_video.mp4
├── process_report.html
├── cut_sources.json
├── provenance.json
├── timing_report.json
├── manifest.json
└── artifacts/
```

工程レポート内の画像・動画は相対参照です。受け渡すときはHTML単体ではなく、提出候補フォルダ全体を維持してください。

## 監視モニター

```bash
uv run python -m agewec_v2.monitor
```

既定で`runtime/runs`を読み取り専用で監視します。

## 実統合テストと差し戻しテスト

以下は実バックエンドを選ぶと生成処理を行うため、設定を確認して使用します。

```bash
uv run python -m agewec_v2.test_pipeline_1cut
uv run python -m agewec_v2.test_revision_routes --all
```

1カット統合テストの結果は`runtime/pipeline_smoke/<run_id>/report.json`へ保存されます。

## 自動テスト

```bash
uv run python -m unittest discover -s tests -v
```

## 旧Runの読み取り互換

旧`workflow_v2`のコード・設定・テスト入口はStage 8で終了しました。実行には正式CLIと`configs/`を使用してください。

旧`workflow_v2/work`と`workflow_v2/submissions`は、過去Runのresume・レポート参照を壊さないため読み取り互換領域として保持しています。新しいコードや生成物は置きません。

## 現在の境界

- VLMによる意味的な映像評価は未接続。技術QAと代表フレーム抽出は実装済み
- 字幕、ナレーション、BGMの最終合成は標準パイプラインへ未統合
- 実動画生成では、選択したバックエンドが要求する素材・APIキー・残高が必要

詳細は[リファクタリング実行ガイド](docs/REFACTORING_EXECUTION_GUIDE.md)と[Phase・パス契約](docs/PHASE_AND_PATH_CONTRACTS.md)を参照してください。
