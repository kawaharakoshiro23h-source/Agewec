# AGEWEC Workflow v2

既存の `src/agewec` を変更せずに検証する、Review Gate型の新ワークフローです。

## パイプライン

```text
Executive Producer
→ Creative Director
→ Writer / Storyboard
→ Asset Curator & Rights
→ Director
→ Image / Video Production
→ Visual QA
→ Post Production
→ Review Board
→ Final Submission Review
→ Provenance
```

各処理ノードの直後には独立したReview Gateがあります。生成処理と承認処理を
分けているため、承認から再開しただけで外部APIやComfyUIが二重実行されません。

Review Gateは工程ごとに次の3種類を選択できます。

- `always`: 毎回、人間の判断を待つ
- `on_exception`: エラー、必須成果物不足、低信頼度の場合だけ待つ
- `never`: 停止せず進む

人間が選べる操作は `approve`、`retry_with_feedback`、`abort` です。

## 既存コードとの関係

- 既存の `src/agewec`、`config.yaml`、`work/` は変更しません。
- 素材選定では、プロジェクト直下の既存 `asset_catalog.json` と
  `assets_dl/` を読み取り専用で参照します。
- v2の設定・生成物・テストはすべて `workflow_v2/` 内に置きます。

## 実行

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run
```

全Review Gateを自動承認して構造だけ検証:

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run --auto
```

テスト:

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m unittest \
  discover -s workflow_v2/tests -v
```

`config.yaml` の `production.backend` は初期状態で `mock` です。
ComfyUIのAPI workflowを保存してノードマッピングを設定した後に
`comfy` へ切り替えます。
