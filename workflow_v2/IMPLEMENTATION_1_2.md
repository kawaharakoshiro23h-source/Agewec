# 処理1・2 実装メモ

## 1. 自律ループの安全上限

`graph_safe.py` は全フェーズの直前に共通の Execution Guard を通します。

設定は `config_llm.yaml` / `config.yaml` の `execution_limits` です。

```yaml
execution_limits:
  max_retries_per_phase: 2
  phase_retry_overrides:
    image_video_production: 2
    visual_qa: 2
    post_production: 2
    review_board: 2
  max_total_phase_executions: 30
  max_runtime_minutes: 60
  on_limit: human_review
```

- `max_retries_per_phase: 2` は初回を含め最大3回
- `phase_retry_overrides` でフェーズ別に変更可能
- `max_total_phase_executions` は全ノード実行数の上限
- `max_runtime_minutes` はワークフロー全体の実行時間上限
- `on_limit: human_review` は上限時に停止し、人間へ確認
- 人間は一度だけ継続するか、中止を選択
- `--auto` 実行中に上限へ達した場合は自動継続せず安全停止

## 2. ComfyUI実接続

`comfy_runtime.py` が次を担当します。

- ComfyUI `/system_stats` と `/object_info` の事前確認
- API形式JSONの検証
- LTXワークフローの入力ノード自動検出
- 入力画像のアップロード
- prompt、negative prompt、解像度、フレーム数、steps、fps、seedの差し替え
- `/prompt` への投入
- `/history/{prompt_id}` の完了監視
- `/view` から生成物を取得
- エラーを共通Review Gateへ返す

初期事故防止のため、実生成はデフォルトで1回につき1動画カットです。

```yaml
production:
  backend: comfy
  max_video_cuts_per_run: 1
  continue_on_cut_error: false
```

特定カットだけ試す場合は次も指定できます。

```yaml
production:
  video_cut_ids: ["1"]
```

現在はComfyバックエンドが有効です。通常実行ではComfyUIへ生成を依頼します。

## Current Comfy status

The API-format workflow is saved and the preflight status is ready. Keep ComfyUI Desktop running when executing the production workflow.


## Tests

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m unittest discover \
  -s workflow_v2/tests -v
```

実装時点で、自律ループ、LLM共通プロバイダ、Comfy API往復、1カット制限を含む
12テストが成功しています。
