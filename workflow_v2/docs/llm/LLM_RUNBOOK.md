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
AGEWEC_LLM_COST_GUARD_ENABLED=true
AGEWEC_LLM_COST_LIMIT_USD=5.0
AGEWEC_LLM_PRICING_MODEL=gpt-4o-mini
AGEWEC_LLM_INPUT_COST_PER_MILLION_USD=0.15
AGEWEC_LLM_OUTPUT_COST_PER_MILLION_USD=0.60
```

LangGraphノードやプロンプトを変更する必要はない。

### ローカル費用上限

OpenAI呼び出し時は`workflow_v2/work/llm_cost_ledger.json`へ、この
ワークフローで発生した推定費用を累積記録する。リクエスト前に入力サイズと
最大出力token分を予約し、累積額が`AGEWEC_LLM_COST_LIMIT_USD`を超える
可能性があればAPIへ送信せず停止する。成功後はAPIが返したtoken usageで
精算し、各LLM結果の`usage.cost_guard`にも記録する。

この上限はAGEWECが記録した利用分だけを対象とし、OpenAIアカウント内の
別プロジェクトや別アプリの支出は取得しない。OpenAI管理画面側の利用上限も
併用すること。

モデルを変更する場合は、`PRICING_MODEL`とinput/output単価も公式料金に
合わせて変更する。単価対象モデルと実モデルが一致しない場合は、安全のため
起動時にエラーにする。

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
