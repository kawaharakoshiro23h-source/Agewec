# AGEWEC Workflow v2

既存の `src/agewec` を変更せずに検証する、Review Gate 型の新ワークフローです。

## 現在できること

- Phase 01–05をLLMまたは決定論的Fallbackで実行
- Phase 05.5でDirector出力をComfyUI向けRequestへ変換
- Phase 06–07で1カットずつ生成、技術QA、承認、部分再実行
- Phase 08でFFmpegによる最終MP4の結合とTechnical QA
- Phase 09をAI審査または`human_only`で運用
- H3は`--auto`でも人間の最終承認を必須化可能
- 実行状態をSQLiteへ保存し、終了・クラッシュ後も同じ`run_id`から再開
- Phase 10で動画、証跡、HTMLレポート、ハッシュManifestを出力

全体図と実装ファイルは
[IMPLEMENTATION_6_10.md](docs/guides/IMPLEMENTATION_6_10.md)を参照してください。

## 構成マップ（どれが本番実装か）

似た名前のモジュールが複数あるため、実行経路を先に示す。

```
run.py
  └─ graph_safe.py           ← 本番グラフ（正）。Review Gate と Execution Guard を挿入
       └─ nodes_runtime.py   ← 本番ノードの入口（正）。実体を下の2つから束ねる
            ├─ nodes_llm.py       Phase 01–05（役割別LLM）
            │    └─ nodes.py      決定論フォールバック＋共有ヘルパ
            └─ pipeline_runtime.py Phase 05.5–10（ComfyUI / ffprobe / FFmpeg / 証跡）
```

| ファイル | 役割 | 直接グラフに繋ぐ？ |
|---|---|---|
| `graph_safe.py` | 本番グラフ定義（正） | — |
| `graph.py` | 互換用ファサード（実体は graph_safe） | 使わない |
| `nodes_runtime.py` | 本番ノードの公開点（正） | **これを使う** |
| `nodes_llm.py` | 役割別LLM（企画〜演出） | 直接は繋がない |
| `nodes.py` | 決定論フォールバック＋共有ヘルパ | 直接は繋がない |
| `pipeline_runtime.py` | 生成・QA・編集・証跡の実処理 | 直接は繋がない |
| `backends/comfy_runtime.py` | ComfyUI APIクライアント | — |
| `media_tools.py` | FFmpeg / ffprobe ラッパ | — |

補足:

- ノードを追加する場合、実装は `nodes_llm.py`（判断系）または
  `pipeline_runtime.py`（実処理系）に置き、`nodes_runtime.py` から公開する。
- `nodes.py` / `nodes_llm.py` の `post_production` は **[LEGACY 未使用]**。
  本番の編集・結合は `pipeline_runtime.post_production`（`ffmpeg_executed`）。

## 安全なローカル確認

```bash
cd /Users/koshiro/Downloads/Agewec
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config.yaml
```

`config.yaml`は`production.backend: mock`なので、ComfyUIを呼ばずテスト用MP4を
生成する。Review Gateで承認しながら全工程を確認できる。

## LLM + ComfyUI（本番・一気通貫）

**これが実際に最後まで通る本番コマンド。** プロジェクトルート
（`/Users/koshiro/Downloads/Agewec`）から実行する。
LM Studio（または`.env`でOpenAI）とComfyUI Desktopを起動しておくこと。

```bash
cd /Users/koshiro/Downloads/Agewec

# 事前の接続確認（任意だが推奨。途中で落ちるのを防ぐ）
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.llm_check
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.comfy_check

# 本番実行（企画→生成→QA→結合→提出Packageまで）
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config_llm.yaml
```

実行開始時に表示される`run_id`は控えておく。Ctrl-C、ターミナル終了、例外停止後は
企画・承認・生成済みカットをSQLiteから復元し、同じrunを再開できる。

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --resume run-xxxxxxxxxx
```

状態は既定で`workflow_v2/work/checkpoints.sqlite`へ保存される。再開時は保存済みの
設定を使うため、`--resume`と`--config`/`--preset`は同時指定できない。

実ComfyUIでは、全カットの`asset.local_path`が存在する必要がある。

### カットを見ながら判断する（Human-in-the-loop）

既定は`autonomy_preset: manual`なので、各フェーズで停止する。
Cut QAで停止した際、ターミナルにレビュー画面のパスが出る。

```
レビュー画面: file:///.../workflow_v2/work/review.html
```

ブラウザで開くと、カットごとに「元画像・選定理由 / 生成動画 / QAフレーム /
プロンプト・演出 / QA結果」が並ぶ（読み取り専用・カットが進むたび更新）。
実物を見たうえで、ターミナルで差し戻し先を選ぶ。

```
[Enter] 承認して次のカットへ
[d] 演出・プロンプトを修正して再生成   → Director
[s] 素材を変更して再生成               → Asset Curator
[g] 生成設定を変更して再生成           → Support Video Creator
[n] 同じ条件で再生成（seed変更）       → Image / Video Production
[a] 中止
```

人間の判断はAIのQA判定より優先される。承認済みの他カットは再生成されない。

`--auto`を付けると自動承認され、この判断機会が飛ぶので、確認したい場合は
付けないこと。自動度を変える場合は`--preset supervised|autonomous`を使う。

### 実行ごとの作業データ（run別）

中間成果物は実行単位で分離される。過去runと混ざらず、再生成前後も比較できる。

```
work/runs/<run_id>/
├── state.json          全工程の入出力・判断
├── events.jsonl        時系列イベント
├── review.html         カットレビュー画面
├── cuts/
│   └── cut_01/
│       ├── source.jpg        使用した元画像
│       ├── request.json      生成条件（prompt/解像度/seed等）
│       ├── attempt_01.mp4    1回目の生成
│       ├── attempt_02.mp4    再生成（seedが変わる）
│       ├── attempt_01_request.json   1回目の生成条件
│       ├── attempt_01_qa.json        1回目のQA結果
│       ├── attempt_01_decision.json  1回目への判断
│       ├── attempt_02_request.json   2回目の生成条件
│       ├── attempt_02_qa.json        2回目のQA結果
│       ├── attempt_02_decision.json  2回目への判断
│       ├── qa_frames/        代表フレーム
│       ├── qa.json           最新のQA結果
│       └── decision.json     最新の判断
└── final/
    └── final_video.mp4  結合後の完成動画
```

提出Packageは `submissions/<run_id>/` に別途出力される（作業データと分けて、
提出物だけを取り出しやすくするため）。

### 提出Packageの中身

`submissions/<run_id>/` に出力される。

| ファイル | 内容 |
|---|---|
| `final_video.mp4` | 完成動画 |
| `process_report.html` | 各工程の入力・判断・生成物のレポート |
| `cut_sources.json` | 使用素材の`asset_id`/`source_url`/`sha256`/選定理由 |
| `artifacts/sources/` | 使用元画像（長辺1280pxへ縮小） |
| `artifacts/cuts/` | カット別の動画 |
| `artifacts/qa/` | QA代表フレーム |
| `provenance.json` | 全実行証跡 |
| `timing_report.json` | フェーズ別の所要時間 |

### 最小の実統合テスト（1カット・既定2秒）

Asset Curatorが選んだローカル画像とDirectorが生成したPromptをそのまま
ComfyUIへ渡し、Phase 07Aの技術QAまでを小型LangGraphで確認する。

```bash
cd /Users/koshiro/Downloads/Agewec
PYTHONPATH=workflow_v2 .venv/bin/python \
  -m agewec_v2.test_pipeline_1cut
```

別カットや元のStoryboard尺を使う場合:

```bash
PYTHONPATH=workflow_v2 .venv/bin/python \
  -m agewec_v2.test_pipeline_1cut --cut-id 2 --seconds 0
```

成功時は動画に加えて、画像・Promptの伝達一致、尺、解像度、FPS、
フレーム数、代表フレームの判定を
`workflow_v2/work/pipeline_smoke/<run_id>/report.json`へ保存する。
VLMによる意味的な画質評価はこのテストの対象外。

## テスト

```bash
cd workflow_v2
../.venv/bin/python -m unittest discover -s tests -v
```

## 現時点の境界

- VLMによる意味的な映像評価は未接続。技術QAと代表フレーム抽出は実装済み
- 字幕、ナレーション、BGMは未設定。映像のみの最終MP4は生成可能
- `config_llm.yaml`で実行する前に全入力画像をローカルへ揃える必要がある
