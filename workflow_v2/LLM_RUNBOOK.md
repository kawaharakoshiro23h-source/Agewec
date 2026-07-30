# LLM Runbook

## LM Studio

1. LM Studioで使用モデルをロードする。
2. Local Serverを起動する。
3. プロジェクトルートの `.env` に次を設定する。

```env
AGEWEC_LLM_ENABLED=true
AGEWEC_LLM_PROVIDER=lmstudio
AGEWEC_LLM_BASE_URL=http://127.0.0.1:1234/v1
AGEWEC_LLM_API_KEY=lm-studio
AGEWEC_LLM_MODEL=<LM Studioに表示されるモデルID>
```

4. 実行する。

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run
```

## Cloud

プロジェクトルートの `.env` を切り替える。

```env
AGEWEC_LLM_ENABLED=true
AGEWEC_LLM_PROVIDER=openai
AGEWEC_LLM_BASE_URL=https://api.openai.com/v1
AGEWEC_LLM_API_KEY=<secret>
AGEWEC_LLM_MODEL=<model-id>
AGEWEC_LLM_STRUCTURED_OUTPUT_MODE=json_schema
AGEWEC_LLM_TOKEN_PARAMETER=max_completion_tokens
```

LangGraphノードやプロンプトを変更する必要はない。

## Safe test without LLM

旧v2設定を明示すると、固定出力でグラフ構造だけを検証できる。

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config.yaml --auto
```

## Failure behavior

`strict_mode: true` の場合、API接続、JSON、スキーマ検証の失敗はモックで
隠されず、その工程のReview Gateへエラーとして渡る。

`strict_mode: false` はデモ・開発専用で、LLM失敗時に従来の決定的出力へ
フォールバックする。
