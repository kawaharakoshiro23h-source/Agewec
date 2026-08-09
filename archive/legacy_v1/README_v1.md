# AGEWEC パイプライン（LangGraph 骨組み）

AGEWEC 2026 向け、AIエージェント動画制作パイプラインの土台。
グラフは端から端まで走り、QAリトライループとチェックポイント（人の承認）が機能する。

## いまの状態（実装の進捗）

| ノード | 状態 | 中身 |
|--------|------|------|
| `planner` | ✅ 実装 | ローカルLLM(LM Studio)で絵コンテを実生成。未接続時はモックに自動フォールバック |
| `asset` | 🟡 一部実装 | 北九州パレットのカタログ照合で実写/生成を振り分け（タグ一致の暫定ロジック）。素材の"選定"エージェントは未実装 |
| `image_gen` | ⬜ モック | ダミー画像。実バックエンド（Nano Banana/ComfyUI 等）は未接続 |
| `qa` | ⬜ モック | 生成カットのNG/リトライは擬似。公式実写は自動合格。VLM判定は未実装 |
| `video_gen` / `audio` / `assembly` | ⬜ モック | ダミー出力。Wan/LTX・VOICEVOX/ACE-Step・FFmpeg は未接続 |
| `provenance` | ✅ 実装 | `work/workflow_log.json` に証跡を出力 |

## 変更履歴

- **v0.1** グラフ骨組み・8ノード（全モック）・QAループ・チェックポイント・証跡出力
- **v0.2** `planner` をローカルLLM(LM Studio)に実接続。`.env`(`LOCAL_*`/`LLM_MODEL`)対応
- **v0.3**（今回）旧 `asset_planner` と `asset_ingest` を **`asset` ノードに統合**。
  北九州パレットの素材カタログ生成（`assets.py`）と、賞ジャンルに合う実写の振り分けを追加。
  カタログが無ければ全カット生成にフォールバック。`Cut` に `source`/`asset_title`/`asset_url` を追加。

## 構成

```
Agewec/                     # プロジェクトルート
├─ config.yaml            # 狙う賞・backend・retry上限・チェックポイントon/off
├─ pyproject.toml         # 依存（uv想定）
├─ .env.example           # APIキー等の雛形（.env は各自作成）
├─ docs/                  # 設計図・スケジュール・チェックリスト・HTMLデモ・資料
└─ src/agewec/
   ├─ state.py            # AgentState / Cut（source/asset_url 等）
   ├─ graph.py            # グラフ定義（配線・条件エッジ）
   ├─ nodes.py            # 各ノード
   ├─ llm.py              # ローカルLLM(LM Studio)クライアント
   ├─ assets.py           # 北九州パレット カタログ生成＋照合（Assetステージ）
   ├─ run.py              # 実行CLI（interrupt→承認→再開）
   └─ backends/           # local/cloud を差し替える層
      ├─ base.py          # Protocol定義
      └─ local.py         # ローカル実装（いまはスタブ）
```

## グラフ

```
planner → asset → image_gen → qa ─(retry)→ image_gen
                                └(continue)→ video_gen → audio
                                             → assembly → provenance → END
```

- `asset` … カタログと照合し各カットを「公式実写 / AI生成」に振り分け。実写カットは image_gen をスキップ
- `qa` … 未合格の生成カットがあれば `image_gen` に戻る（`max_retries` まで）。公式実写は自動合格
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

## Assetステージ：北九州パレットのカタログ生成

`asset` ノードは `asset_catalog.json` があれば実写を照合して使う。カタログはネット接続して先に作る:

```bash
uv run python -m agewec.assets            # ギャラリーを巡回して asset_catalog.json を生成
uv run python -m agewec.assets --pages 3  # まず3ページだけお試し
```

- カタログが無い状態で `run` すると、全カット「AI生成」に自動フォールバック（＝今のサンドボックス動作）。
- カタログがあると、狙う賞のジャンル（例: 夜景賞→「イルミネーション・夜景」）に合う実写を各カットに割り当てる。
- 取得コードは公式サイトのHTML構造に依存。初回は取れ方を確認し、必要なら `assets.parse_gallery()` を微調整。利用は「観光振興用途」の規約に従うこと。

## 次段階（実装で埋める箇所）

| 箇所 | やること |
|------|----------|
| `nodes.asset`（選定） | 実写候補の"選定"をキュレーター/プロデューサー・エージェントに置換（現状はタグ一致の暫定） |
| `backends/local.py` `generate_image` | Nano Banana(Gemini画像) / ComfyUI / diffusers で画像生成 |
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
