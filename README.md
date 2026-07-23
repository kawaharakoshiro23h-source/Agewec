# AGEWEC パイプライン（LangGraph 骨組み）

AGEWEC 2026 向け、AIエージェント動画制作パイプラインの土台。
グラフは端から端まで走り、QAリトライループとチェックポイント（人の承認）が機能する。
**各ノードはモック出力**（実モデル接続は次段階）。

## 構成

```
Agewec/                     # プロジェクトルート
├─ config.yaml            # 狙う賞・backend・retry上限・チェックポイントon/off
├─ pyproject.toml         # 依存（uv想定）
├─ .env.example           # APIキー等の雛形（.env は各自作成）
├─ docs/                  # 設計図・スケジュール・チェックリスト・HTMLデモ・資料
└─ src/agewec/
   ├─ state.py            # AgentState / Cut
   ├─ graph.py            # グラフ定義（配線・条件エッジ）
   ├─ nodes.py            # 8ノード（モック）
   ├─ run.py              # 実行CLI（interrupt→承認→再開）
   └─ backends/           # local/cloud を差し替える層
      ├─ base.py          # Protocol定義
      └─ local.py         # ローカル実装（いまはスタブ）
```

## グラフ

```
planner → asset_planner → image_gen → qa ─(retry)→ image_gen
                                        └(continue)→ video_gen → audio
                                                     → assembly → provenance → END
```

- `qa` … 未合格カットがあれば `image_gen` に戻る（`max_retries` まで）
- `qa` / `assembly` … `interrupt` で停止し、人の承認を待つチェックポイント

## 実行

```bash
cd ~/Downloads/Agewec           # プロジェクトルート
uv venv && source .venv/bin/activate
uv pip install -e .
uv run python -m agewec.run --auto   # 自動承認で通し実行
uv run python -m agewec.run          # 各チェックポイントで承認を尋ねる
```

実行後、`work/workflow_log.json`（証跡）と `work/*.txt`（モック成果物）が出力される。

## 次段階（実装で埋める箇所）

| 箇所 | やること |
|------|----------|
| `nodes.planner` | ローカルLLM(LM Studio, OpenAI互換)で絵コンテ生成 |
| `backends/local.py` `generate_image` | ComfyUI(HTTP) or diffusers で FLUX |
| `backends/local.py` `image_to_video` | ComfyUI で Wan2.1 1.3B / LTX-Video (GGUF) |
| `nodes.qa` | VLM で画像とpromptの整合を判定 |
| `backends/local.py` `tts`/`bgm` | VOICEVOX / ACE-Step |
| `nodes.assembly` | FFmpeg で結合・字幕・BGMミックス |
| `backends/cloud.py` | Kling/Veo/Runway（フォールバック、要有料枠） |

## メモ: ポート（フロントを作る場合）

現状の骨組みは CLI 実行のみでフロントのポートは不要。
**将来 Web UI を作る際は、フロントに 3000 を使わないこと**（別プロジェクトで 3000 使用中のため衝突する）。
`3200` など空いている番号を使う。例:

```bash
# Next.js/Vite など: 3000 ではなく 3200 を指定
npm run dev -- --port 3200
```

参考の既定ポート（重複回避のため控え）:
- フロント（将来）… 3200 を推奨（3000は避ける）
- FastAPI（将来）… 8000
- LM Studio … 1234 / ComfyUI … 8188 / VOICEVOX … 50021
```
