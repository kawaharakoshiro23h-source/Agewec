# Legacy runtime data

このディレクトリは、Stage 8以降は旧Runの読み取り互換領域です。

- `work/`: 旧Run、旧checkpoint、途中成果物
- `submissions/`: 旧レイアウトで生成された提出候補

現行コード・設定・テスト・Comfy workflowの正本は、それぞれ`src/agewec_v2/`、`configs/`、`tests/`、`workflows/`にあります。このディレクトリへ新しい実装や生成物を追加しません。

新規実行:

```bash
uv run agewec --config configs/config_local.yaml
```

完了済み旧Runは、正式CLIの`--resume`から旧checkpointを自動検出して読み取れます。
