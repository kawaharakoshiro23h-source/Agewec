# AGEWEC Pipeline Guide

## パイプライン

```text
Executive Producer
→ Creative Director
→ Writer / Storyboard
→ Asset Curator & Rights
→ Director
→ Support Video Creator
→ Image / Video Production
→ Cut Visual QA
→ Sequence QA
→ Post Production
→ Review Board / Final Submission Review
→ Provenance
```

各判断工程のReview Gateでは、承認・フィードバック付き差し戻し・中止を選べます。生成処理と承認処理は分離されているため、承認から再開しただけで外部動画APIを二重実行しません。

## 正式配置

- 実装: `src/agewec_v2/`
- 設定: `configs/`
- テスト: `tests/`
- 新規実行データ: `runtime/`
- 確定提出物: `deliverables/`
- 旧Run読み取り領域: `workflow_v2/work`、`workflow_v2/submissions`

## 安全なmock実行

外部LLM・動画生成APIを使わずに全工程を確認します。

```bash
uv run agewec --config configs/config_local.yaml
```

Review Gateを自動承認する場合:

```bash
uv run agewec --config configs/config_local.yaml --auto
```

H3が人間確認必須の場合は、`--auto`でも最終提出前に停止します。

## LLM・動画生成を使う実行

`.env`と`configs/config_llm.yaml`のバックエンド、モデル、対象カット、費用上限を確認してから実行します。

```bash
uv run agewec --config configs/config_llm.yaml
```

## 再開

```bash
uv run agewec --resume run-xxxxxxxxxx
```

新規checkpointは`runtime/checkpoints/checkpoints.sqlite`へ保存されます。完了済み旧Runは旧checkpointから読み取り可能です。

## 監視モニター

```bash
uv run python -m agewec_v2.monitor
```

既定では`runtime/runs`を読み取り専用で表示します。

## テスト

```bash
uv run python -m unittest discover -s tests -v
```

より詳しい構成と注意点は、ルートの`README.md`と`docs/PHASE_AND_PATH_CONTRACTS.md`を参照してください。
