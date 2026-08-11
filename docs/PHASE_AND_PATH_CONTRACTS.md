# Phase・パス契約

最終更新: 2026-08-09
対象: 第2〜6段階（巨大ファイル分割・runtime集約・正式配置後の境界固定）

## 目的

`pipeline_runtime.py`や`nodes_llm.py`を分割しても、保存先やPhase間の状態更新を変えないための契約を定める。

## パスの所有者

パス解決は`src/agewec_v2/paths.py`の`RuntimePaths`へ集約する。

| 種類 | 基準ディレクトリ | 現在の既定値 |
| --- | --- | --- |
| runtime/work | project root | `runtime` |
| runs | runtime root | `runtime/runs` |
| submissions | runtime root | `runtime/submissions` |
| assets | project root | `assets_dl` |
| asset catalog | project root | `asset_catalog.json` |
| checkpoints | runtime root | `runtime/checkpoints/checkpoints.sqlite` |
| shared provenance | runtime root | `runtime/provenance.json` |
| configs | project root | `configs` |
| prompts | package root | `src/agewec_v2/prompts` |

`paths.runtime_dir`を持つ設定は、実行生成物の相対パスをruntime root基準で解決する。`runtime_dir`を持たず旧キーを明示する保存済み設定は、互換性のためworkflow root基準の旧契約を維持する。Phase実装へ個別のパス計算を追加しない。

## Phase契約

機械可読な一覧は`src/agewec_v2/phase_contracts.py`の`PHASE_CONTRACTS`を正とする。全Phaseに、入力・出力・副作用を宣言する。

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

## 第3段階後の実装配置

| モジュール | 責務 |
| --- | --- |
| `phases/common.py` | Phase間で共有する純粋ヘルパとJSON保存 |
| `phases/support_video.py` | Phase 05.5・バックエンド固有Requestの構築 |
| `phases/production.py` | Phase 06・動画生成、課金前ガード、入力画像前処理 |
| `phases/cut_qa.py` | Phase 07A・カット技術QAと差し戻し確定 |
| `phases/sequence_qa.py` | Phase 07B・全カットの編集準備確認 |
| `phases/post_production.py` | Phase 08〜09・FFmpeg編集とReview Board |
| `phases/reporting.py` | 制作過程レポートの集計とHTML/Markdown描画 |
| `phases/provenance.py` | Phase 10・証跡と提出Package生成 |

`nodes_runtime.py`は上記を直接参照する。`pipeline_runtime.py`は旧import利用者のための再エクスポートだけを行い、新規実装は追加しない。互換窓口は第6段階まで維持し、第8段階で利用箇所を監査して削除可否を決める。

## 第4段階後の役割・フォールバック配置

LLMを利用する役割処理は`roles/`、LLMを利用できない場合の決定的処理は`fallbacks/`へ分離する。

| モジュール | 責務 |
| --- | --- |
| `roles/common.py` | LLM設定、RoleRunner呼び出し、エラー・metadata共通処理 |
| `roles/project.py` | Executive Producer、Creative Director |
| `roles/storyboard.py` | Writer / Storyboardと固定絵コンテ処理 |
| `roles/assets.py` | 素材候補作成、ショートリスト、明示指定、Asset Curator |
| `roles/director.py` | Director、生成方式・素材整合の構造化検証 |
| `roles/downstream.py` | Post Production、Review Board、ProvenanceのLLM役割 |
| `fallbacks/common.py` | state、設定、feedback、metadataの決定的共通処理 |
| `fallbacks/planning.py` | 企画・コンセプト・絵コンテの決定的フォールバック |
| `fallbacks/assets.py` | 素材選定の決定的フォールバック |
| `fallbacks/director.py` | 演出計画の決定的フォールバック |
| `fallbacks/legacy_media.py` | 旧決定的メディア処理の互換実装 |

依存方向は`nodes_runtime.py → phases/・roles/ → fallbacks/`とする。`roles/`と`fallbacks/`から`nodes_llm.py`、`nodes.py`、`nodes_runtime.py`へ逆importしてはならない。

`nodes_llm.py`と`nodes.py`は旧import利用者向けの再エクスポートだけを行い、新しいロジックを追加しない。互換窓口は第6段階まで維持し、第8段階で利用箇所を監査して削除可否を決める。

## 第5段階後のruntime・checkpoint契約

1. 新規Runの途中成果物、提出候補、checkpointは`runtime/`へ出力する。
2. `runtime/`はGit追跡外とし、ソースコードと実行生成物を混在させない。
3. 旧`workflow_v2/work`と`workflow_v2/submissions`は移動・削除せず、過去Runの参照整合性を維持する。
4. 新checkpointに同じrun_idがあれば新checkpointを優先する。
5. 新checkpointに無く旧checkpointにある完了済みrunは、自動検出して読み取り可能とする。
6. 未完了の旧runは、state内の移行前絶対パスを誤用しないよう継続を拒否する。再開には専用のstate移行が必要となる。
7. 新runtimeの未完了runは、同じrun_idから通常どおり再開できる。

## 第8段階後の正式配置・互換契約

1. 現行パッケージの正本は`src/agewec_v2/`とする。
2. 設定は`configs/`、テストは`tests/`、仕様書は`docs/`、補助処理は`scripts/`を正とする。
3. 旧`workflow_v2`のコード・設定・テスト・workflow用シンボリックリンクは廃止し、正本を二重公開しない。
4. 旧`workflow_v2/work`・`workflow_v2/submissions`だけは過去Run参照のため保持するが、新規出力先にはしない。
5. CLIは`agewec`エントリポイントを正とし、設定は`configs/`から明示する。
6. promptとモニターUIはパッケージ相対で探索し、リポジトリ直下の旧配置を仮定しない。
7. `nodes.py`、`nodes_llm.py`、`pipeline_runtime.py`、`graph.py`は、テストと既存Python importが残る薄い再エクスポート層として維持する。新規ロジックは追加しない。
