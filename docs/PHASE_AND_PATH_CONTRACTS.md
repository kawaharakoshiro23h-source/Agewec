# Phase・パス契約

最終更新: 2026-08-08  
対象: 第2段階（巨大ファイル分割前の境界固定）

## 目的

`pipeline_runtime.py`や`nodes_llm.py`を分割しても、保存先やPhase間の状態更新を変えないための契約を定める。

## パスの所有者

パス解決は`workflow_v2/agewec_v2/paths.py`の`RuntimePaths`へ集約する。

| 種類 | 基準ディレクトリ | 現在の既定値 |
| --- | --- | --- |
| work | workflow root | `workflow_v2/work` |
| runs | work root | `workflow_v2/work/runs` |
| submissions | workflow root | `workflow_v2/submissions` |
| assets | project root | `assets_dl` |
| asset catalog | project root | `asset_catalog.json` |
| checkpoints | workflow root | `workflow_v2/work/checkpoints.sqlite` |
| prompts | package root | `agewec_v2/prompts` |

相対パスの意味は現状維持とする。将来`runtime/`へ移すときは設定値を変え、Phase実装へ新しいパス計算を追加しない。

## Phase契約

機械可読な一覧は`workflow_v2/agewec_v2/phase_contracts.py`の`PHASE_CONTRACTS`を正とする。全Phaseに、入力・出力・副作用を宣言する。

特に次の規約を壊してはならない。

1. 初回または一部しかRequestがない状態では、`target_cut_id`があっても全カットのProductionRequestを構築する。
2. 全カット分のRequestが揃っているときだけ、指定カットの差分更新を行う。
3. 同一条件で動画だけを再生成する場合は既存Artifactを参照できる。
4. 素材・演出・生成設定へ戻す場合は、対象カットの旧Artifactを下流で再利用させない。
5. 人間による明示承認はAIのQA判定を上書きできる。
6. 課金前に停止したRequestと、課金後に生成済みのArtifactを同じ状態として扱わない。

## 分割時の確認

- 関数の引数・戻り値・stateのキーを変更しない。
- 互換モジュールから旧importを一定期間再エクスポートする。
- Phase単体テストに加え、差し戻しから下流までの結合テストを実行する。
- 実APIを使わずmockで確認する。
- 全テスト成功後にのみ次のPhaseを移す。
