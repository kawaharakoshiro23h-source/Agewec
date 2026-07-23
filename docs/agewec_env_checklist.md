# AGEWEC — 必要環境・ライブラリ一覧（Macローカル前提）

実装前の準備リスト。前提: MacBook Pro M5 / 32GB（Apple Silicon・Metal/MPS）。※インストールはまだ行わない。

---

## 0. ベース環境

- **macOS**（M5 / Apple Silicon）
- **Homebrew** — CLIツール導入用
- **Python 3.10〜3.12**（ComfyUI・LangGraphの対応版）
- **仮想環境ツール** — `venv` または `uv` / `conda`（プロジェクト分離）
- **git** — ComfyUI・カスタムノード取得
- **FFmpeg** — 動画結合・字幕・音声ミックス（`brew install ffmpeg`）
- （任意）**Xcode Command Line Tools** — ビルド依存

---

## 1. オーケストレーション（設計図の中枢）

| 用途 | ライブラリ | 備考 |
|------|-----------|------|
| エージェント制御 | `langgraph` | グラフ・状態・チェックポイント/interrupt |
| LLM連携基盤 | `langchain`, `langchain-openai` | LM StudioのOpenAI互換APIを叩く |
| 状態スキーマ | `pydantic` | Cut/AgentStateの型定義 |
| HTTP呼び出し | `httpx` または `requests` | ComfyUI/VOICEVOX/ACE-Step へ |
| 設定管理 | `pyyaml` | backend切替・retry上限などのconfig |
| ログ・証跡 | 標準 `logging` + `json` | Provenanceノードの出力 |

---

## 2. ノード別の依存

### 1) Planner / 4) QA（テキスト・視覚判定）
- **LM Studio**（導入済み）— ローカルLLMをOpenAI互換サーバとして起動
- QAで画像を見る場合: **視覚対応モデル（VLM）**をLM Studioで用意（例: Qwen2-VL系）。ローカルVLMは精度が落ちる点に注意

### 2) Asset Planner
- 追加ライブラリ不要（LLM＋ルール）
- 北九州パレット公式素材を使う場合はダウンロード運用のみ

### 3) Image Gen（画像生成）
- **ComfyUI** 本体（git clone）
- **PyTorch（MPS対応版）** — Apple Silicon向け
- カスタムノード: **ComfyUI-GGUF**（量子化モデル読み込み）
- 画像モデル: **FLUX**（重い→GGUF量子化版推奨）／軽量代替（SD系）も選択肢

### 4→3 QAループ
- 画像評価用の軽い画像処理: `Pillow`, （任意）`opencv-python`

### 5) Video Gen（画像→動画）
- ComfyUI カスタムノード:
  - **ComfyUI-WanVideoWrapper**（Wan 2.1 用）
  - **ComfyUI-LTXVideo**（LTX-Video 用）
- モデル（GGUF量子化版）: **Wan 2.1 1.3B**, **LTX-Video**
- 注意: Metalは**FP8非対応**→GGUF必須。2〜4秒・480〜512pが現実ライン

### 6) Audio（音声・BGM）
- **VOICEVOX**（ナレーション/TTS）— アプリ版エンジン、または `voicevox_core`
- **ACE-Step**（導入済み・BGM生成、商用可ライセンス）
- 音声処理（任意）: `pydub`（音量調整・整形、内部でFFmpeg利用）

### 7) Assembly（結合）
- **FFmpeg**（クリップ結合・字幕焼き込み・BGMミックス）
- Python側から呼ぶなら `ffmpeg-python` ラッパ（任意、直接subprocessでも可）

### 8) Provenance（証跡）
- 追加ライブラリ不要（`json` / ファイルコピー）
- スクショ自動化する場合: `mss` など（任意）

---

## 3. Webアプリ化する場合（任意・後回し可）

| 層 | 技術 | 備考 |
|----|------|------|
| バックエンド | `fastapi`, `uvicorn` | LangGraph実行をAPI化 |
| リアルタイム更新 | `websockets` / SSE | ノード状態のライブ表示 |
| フロント | 素のHTML/JS（試作と同方式）or React | 最小構成でよい |

---

## 4. ダウンロードが必要なモデル（容量大・先に確保）

- 画像: **FLUX**（GGUF量子化版）
- 動画: **Wan 2.1 1.3B**（GGUF）／**LTX-Video**（GGUF）
- LLM: LM Studio上のモデル（導入済み想定）＋必要ならVLM
- 音声: VOICEVOX話者データ、ACE-Step重み（導入済み）

> モデルDLは時間・ストレージを食う。1TBに余裕はあるが、GGUF版で容量を抑える。7/25の最優先タスク。

---

## 5. アカウント/外部（フォールバック用・任意）

- 有料クラウド動画（詰まった時のみ）: Kling / Veo / Runway のいずれか
  - ※無料枠はウォーターマーク＋商用不可。受賞ライン用は有料枠が前提
- 動画共有: Google Drive / OneDrive / iCloud（提出用の共有リンク）

---

## メモ（判断が要る箇所）

- **FLUXローカルは重い**。画像だけ軽量モデルに落とすか、画像/動画のどちらをボトルネックにするか要検討。
- **QAの視覚判定**をローカルVLMでやるか、精度優先でここだけ有料APIにするかは、7/27に実物を見て決める。
- クラウドは**商用利用可否**が受賞時に効くので、使うツールのライセンスは提出前（7/30）に最終確認。
