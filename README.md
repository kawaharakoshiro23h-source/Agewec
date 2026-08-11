# AGEWEC Pipeline

企画から動画生成、品質確認、編集、証跡・工程レポート出力までをLangGraphで実行する、Human-in-the-loop型の映像制作パイプラインです。

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
workflow_v2/         旧コマンドと過去Runのための一時互換領域
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

## 旧コマンドの一時互換

Stage 8で利用箇所を監査するまで、次の旧入口も維持します。

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config.yaml
```

旧`workflow_v2/work`と`workflow_v2/submissions`は、過去Runの参照を壊さないため移動・削除していません。

## 現在の境界

- VLMによる意味的な映像評価は未接続。技術QAと代表フレーム抽出は実装済み
- 字幕、ナレーション、BGMの最終合成は標準パイプラインへ未統合
- 実動画生成では、選択したバックエンドが要求する素材・APIキー・残高が必要

詳細は[リファクタリング実行ガイド](docs/REFACTORING_EXECUTION_GUIDE.md)と[Phase・パス契約](docs/PHASE_AND_PATH_CONTRACTS.md)を参照してください。
