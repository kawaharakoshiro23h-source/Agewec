# AGEWEC Workflow v2

既存の `src/agewec` を変更せずに検証する、Review Gate 型の新ワークフローです。

## 現在できること

- Phase 01–05をLLMまたは決定論的Fallbackで実行
- Phase 05.5でDirector出力をComfyUI向けRequestへ変換
- Phase 06–07で1カットずつ生成、技術QA、承認、部分再実行
- Phase 08でFFmpegによる最終MP4の結合とTechnical QA
- Phase 09をAI審査または`human_only`で運用
- H3は`--auto`でも人間の最終承認を必須化可能
- Phase 10で動画、証跡、HTMLレポート、ハッシュManifestを出力

全体図と実装ファイルは
[IMPLEMENTATION_6_10.md](docs/guides/IMPLEMENTATION_6_10.md)を参照してください。

## 安全なローカル確認

```bash
cd /Users/koshiro/Downloads/Agewec
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config.yaml
```

`config.yaml`は`production.backend: mock`なので、ComfyUIを呼ばずテスト用MP4を
生成する。Review Gateで承認しながら全工程を確認できる。

## LLM + ComfyUI

LM StudioとComfyUI Desktopを起動し、接続確認後に実行する。

```bash
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.llm_check
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.comfy_check
PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.run \
  --config workflow_v2/config_llm.yaml
```

実ComfyUIでは、全カットの`asset.local_path`が存在する必要がある。

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
