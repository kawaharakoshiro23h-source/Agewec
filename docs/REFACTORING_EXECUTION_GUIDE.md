# AGEWEC リファクタリング実行ガイド

最終更新: 2026-08-09
状態: **第0〜6段階完了・第7段階着手前**
対象: AGEWEC提出後の構造整理と保守性改善

## 1. この文書の目的

提出時点の成果物と動作を壊さず、AGEWECプロジェクトを段階的に整理するための実行基準を定める。

このリファクタリングでは、次を最優先とする。

- 提出済み成果物を変更しない
- 現在のワークフローの振る舞いを変えない
- Runway APIを誤って呼び出さない
- 移動とロジック変更を同じ差分に混ぜない
- 各段階をテスト・コミット・停止の単位にする
- 問題発生時に直前の安定状態へ戻せるようにする

## 2. 作業対象の固定

### 原本・保存版

```text
/Users/koshiro/Downloads/Agewec
```

提出時点の保存版。**リファクタリングでは一切変更しない。** 比較・参照専用とする。

`/Users/koshiro/Documents/Agewec`は上記フォルダへのシンボリックリンクであり、別のコピーではない。

### リファクタリング作業版

```text
/Users/koshiro/Downloads/Agewecのコピー
```

実装・移動・テスト・コミットは、すべてこちらで行う。

開始前に、ターミナルの`pwd`と`git rev-parse --show-toplevel`が作業版を指していることを必ず確認する。

## 3. 開始時の状態

リファクタリング開始時は、提出物としては整理されている一方、開発環境では旧版・現行版・実行生成物が混在していた。

```text
Agewec/
├── AGEWEC2026_提出資料/     # 確定提出物
├── workflow_v2/             # 現行パイプライン
│   ├── agewec_v2/           # 本番コード
│   ├── tests/               # テスト
│   ├── docs/                # v2仕様書
│   ├── work/                # Run・チェックポイント・途中成果物
│   └── submissions/         # 自動生成された提出候補
├── src/agewec/              # 旧版コード
├── docs/                    # 初期資料
├── work/                    # 旧版作業領域
├── assets_dl/               # ダウンロード素材
├── other/                   # 完成動画の派生版・応募フォーム案
├── config.yaml              # 旧版設定
├── workflow_v2/config_llm.yaml
└── pyproject.toml
```

### 実測上の重要事項

- 大容量の主因はGitではなく、`work/`・`submissions/`・素材・動画などのローカル生成物
- これらの多くはすでに`.gitignore`対象
- `src/agewec`は現行`workflow_v2`から参照されていない旧版
- `pyproject.toml`は旧版`src/agewec`をインストール対象にしているため、editable installで旧版が選ばれる危険がある
- `pipeline_runtime.py`と`nodes_llm.py`が大きく、Phase間の状態変更を追いにくい
- `work/checkpoints.sqlite`と既存Runには、旧配置や絶対パスへの依存が含まれる可能性がある

### 第6段階完了後の正式配置

```text
Agewecのコピー/
├── src/agewec_v2/       # 現行パイプラインの正本
├── configs/             # local mock / LLM・動画API設定
├── tests/               # 正式テスト入口
├── docs/                # 仕様・運用資料
├── scripts/             # 補助スクリプト
├── workflows/           # ComfyUI API workflow
├── runtime/             # 新規Run・checkpoint・提出候補（Git追跡外）
├── archive/legacy_v1/   # 旧版
└── workflow_v2/         # 旧入口と過去Runだけを保持する一時互換領域
```

新規実装は`src/agewec_v2/`へ追加する。`workflow_v2`配下のコード・設定・テスト・workflowは正本へのシンボリックリンクであり、二重編集しない。

## 4. 理想状態

```text
Agewec-refactor/
├── README.md
├── pyproject.toml
├── configs/
│   ├── config_llm.yaml
│   └── config_local.yaml
├── src/
│   └── agewec_v2/
│       ├── graph/
│       ├── phases/
│       ├── backends/
│       ├── llm/
│       ├── review/
│       ├── reporting/
│       └── prompts/
├── tests/
├── docs/
├── scripts/
├── runtime/                 # Git追跡外
│   ├── assets/
│   ├── runs/
│   ├── submissions/
│   ├── checkpoints/
│   └── cache/
├── deliverables/
│   ├── AGEWEC2026/
│   └── variants/
└── archive/
    └── legacy_v1/
```

当面はパッケージ名`agewec_v2`を維持する。`agewec`への改名は、このリファクタリングの対象外とする。

## 5. 今回変更しないもの

- 各AIエージェントの役割やプロンプト内容
- LangGraphのPhase順序・差し戻し経路
- Runway・ComfyのAPI仕様
- モデル、料金、生成尺、解像度の決定方法
- 人間確認の判断ルール
- レポートに記録する情報の意味
- 提出済み動画・レポート・証跡の内容

構造整理中に機能上の問題を発見した場合は、その場で混ぜて直さず、別課題として記録する。

## 6. 実行順序

### 第0段階: ベースライン固定

目的: 変更前の正常状態と提出物の完全性を記録する。

実施内容:

1. 原本と作業版の絶対パスを確認
2. Gitブランチ、コミットID、`git status`を記録
3. 現在の全テスト件数と結果を記録
4. mockの1カット実行を行い、結果を保存
5. 提出済み動画・レポート・証跡のSHA-256を記録
6. Runway APIを呼ばない設定・環境であることを確認

注意:

- 提出済みファイルはSHA-256一致を完了条件にできる
- 再生成したMP4はコンテナの時刻情報などでハッシュが変わる可能性がある
- mock再実行は、尺・解像度・ストリーム・代表フレームなどの意味的な一致で比較する

完了条件:

- 全テスト成功
- 原本と作業版の役割が明確
- 提出物の基準ハッシュが保存済み
- mockベースラインが保存済み

### 第1段階: 旧版隔離とパッケージ設定修正

目的: 旧版が誤ってインストール・実行される状態を解消する。

実施内容:

1. `src/agewec`を`archive/legacy_v1/`へ移動
2. `pyproject.toml`のインストール対象を現行`agewec_v2`へ変更
3. `prompts/*.md`をpackage dataとして同梱する設定を確認・追加
4. editable install後に現行CLIとprompt読込を確認

この4項目は同じ段階・同じコミットで扱う。旧版だけ移動し、インストール設定が壊れた中間状態を残さない。

完了条件:

- editable installが成功
- importされるのが現行`agewec_v2`
- インストール後も全promptを読み込める
- 全テスト成功

### 第2段階: 分割前の契約とパス管理

目的: 巨大ファイルを安全に分割するための境界を固定する。

実施内容:

1. work、runs、submissions、assets、checkpoints等のパス解決を一元化
2. 各Phaseの入力・出力・副作用を明文化
3. `target_cut_id`が有効なPhaseと期間を定義
4. targeted revision時に更新するカットと維持するカットを定義
5. `production_requests`、`production_artifacts`、QA結果の保持・破棄条件を定義
6. 差し戻し経路の結合テストを追加
7. Phase単体ではなく、Phase間のつなぎ目を検証するテストを追加

最低限固定する規約:

- 初回のProductionRequest構築では全カットを作る
- 全カット分の土台がある場合だけ、対象カットの差分更新を許可する
- 人間の承認はAIのQA判定を上書きできる
- 成果物参照を破棄する場合は、下流で再利用不能になることを明示する
- 課金前ガードと課金後成果物の状態を区別する

完了条件:

- パスが1か所から解決される
- 主要なPhase間規約がテストで保護される
- 振る舞いの変更なし
- 全テスト成功

### 第3段階: `pipeline_runtime.py`の分割

目的: Phaseごとの責務と変更範囲を明確にする。

分割候補:

```text
src/agewec_v2/phases/
├── support_video.py
├── production.py
├── cut_qa.py
├── sequence_qa.py
├── post_production.py
├── provenance.py
└── reporting.py
```

手順:

1. 共有型・純粋関数・パス解決を先に抽出
2. 1Phaseずつ移動
3. 元の`pipeline_runtime.py`から再エクスポートして互換性を維持
4. Phase移動ごとに関連テストを実行
5. 一まとまりごとに全テストを実行

この段階では関数名、引数、戻り値、状態更新の意味を変えない。

完了条件:

- 本番グラフの参照先が変わっても動作が同じ
- `pipeline_runtime.py`は互換窓口としてのみ残る
- 全テストとmock実行が成功

### 第4段階: LLM・決定論ノードの分割

目的: 役割別処理と共有ロジックを分離する。

手順:

1. `_shortlist_candidates`等の共有ヘルパを先に専用モジュールへ抽出
2. `nodes_llm.py`を役割別モジュールへ分割
3. `nodes.py`を共通処理と決定論フォールバックへ分離
4. `nodes_runtime.py`を本番ノードの互換窓口として維持
5. LLM失敗時のフォールバックと人間フィードバック経路を確認

共有ヘルパを各役割ファイルへ複製しない。循環importが発生した場合は先へ進まず、依存方向を見直す。

完了条件:

- 役割ごとの依存が一方向
- 共有ヘルパの重複なし
- structured validationとfallbackが従来どおり
- 全テスト成功

### 第5段階: 実行生成物の`runtime/`集約

目的: ソースコードと、実行ごとに変化するデータを分離する。

実施内容:

1. 新しいパス設定を使って新規Runが`runtime/`へ出力されるようにする
2. 新規checkpointで中断・resumeを検証
3. 既存`checkpoints.sqlite`を読み取り、完了済みrunの復元を検証
4. SQLite内とstate内の絶対パス依存を確認
5. 旧データは移動せず、新規Runだけを`runtime/`へ出力
6. 新checkpointに無いrunは旧checkpointから安全に読み取る

重要:

- `work/checkpoints.sqlite`だけを単純移動・コピーして既定化しない
- 過去RunのJSON・HTML・checkpointには絶対パスが含まれ得る
- Git追跡外であることと、安全に移動できることは別問題
- 提出Runと証跡は、不要と判断しても即削除しない
- 未完了の旧runは専用のstate移行なしに継続しない

完了条件:

- 新規Runが`runtime/`で完走
- 中断した新規Runをresume可能
- 選定した完了済み既存Runを読み取り可能
- 未完了の旧runは危険な継続を明示的に拒否
- レポート内の画像・動画参照が壊れていない

### 第6段階: 正式ディレクトリへの機械的移設

目的: 現行v2をリポジトリの正式な実装として配置する。

実施内容:

1. `workflow_v2/agewec_v2`を`src/agewec_v2`へ移動
2. tests、docs、scripts、configsをルート配下へ統合
3. import、CLI、設定探索、prompt探索を更新
4. 旧コマンドには一時的な互換入口を残す
5. 移動だけのコミットとし、ロジックを変更しない

完了条件:

- 新旧CLI入口の必要範囲が動作
- editable install成功
- 全テスト成功
- mock実行成功

### 第7段階: 提出物・派生物・文書の整理

目的: 確定成果物と作業生成物を明確に区別する。

整理方針:

```text
deliverables/
├── AGEWEC2026/             # 確定提出物
└── variants/               # 字幕・BGM等の派生版

docs/submission/            # 応募フォーム案・提出関連文書
archive/                    # 旧版・保存目的の資料
```

実施前後で提出物のSHA-256を比較する。内容が変わった場合は完了扱いにしない。

### 第8段階: 最終検証と互換窓口の整理

実施内容:

1. 全テスト実行
2. editable install確認
3. mockの1カット実行
4. CLI、レポート、モニター、checkpoint resume確認
5. 提出物のハッシュ照合
6. 原本フォルダが未変更であることを確認
7. READMEと構成図を新構成へ更新
8. 互換窓口の利用箇所を検索し、削除可否を判断

互換窓口は無期限に残さない。ただし、すべての呼び出し元とテストが新経路へ移行したことを確認するまで削除しない。

## 7. 作業の区切り

各段階は、必ず次の単位で終了する。

```text
1. 対象範囲を確認
2. 変更前テスト
3. その段階だけ実装
4. 関連テスト
5. 全テスト
6. git diff確認
7. 1段階1コミット
8. 結果を記録して停止
```

複数段階を1コミットにまとめない。次段階は、前段階の結果を利用者が確認してから開始する。

推奨する実行セッションの分割:

| セッション | 対象 |
|---|---|
| A | 第0〜2段階 |
| B | 第3段階 |
| C | 第4段階 |
| D | 第5〜6段階 |
| E | 第7〜8段階 |

## 8. 致命的になり得る注意点

### 原本を作業対象にしない

コマンド実行前に必ず作業版の絶対パスを確認する。`Documents/Agewec`は原本へのリンクなので、作業場所として使用しない。

### API課金を発生させない

テスト・mock・dry-run以外の本番バックエンドを使用しない。実Runway検証は、利用者が金額と対象カットを明示的に承認した場合だけ行う。

### checkpoint移動を軽視しない

SQLiteファイルが開けるだけでは移行成功ではない。保存state内の絶対パス、生成物、レビューHTMLまで確認する。

### promptをパッケージから落とさない

ソースツリーでは動いても、インストール後にMarkdownが欠落するとLLM実行時に失敗する。editable installと通常のpackage dataの両方を意識する。

### 分割だけで設計改善とみなさない

関数を別ファイルへ移すだけでは、Phase間の状態バグは減らない。入力・出力・副作用・保持条件を先に定義する。

### 提出物の再現性と完全性を混同しない

- 完全性: 提出済みファイルが移動前後で同じかをSHA-256で確認
- 再現性: パイプライン出力が同等の仕様・内容になるかを別途確認

提出動画はワークフロー外の字幕・BGM・リサイズ処理を含む可能性があり、パイプライン再実行で同一バイト列になることを要求しない。

### 秘密情報を共有しない

作業コピーには`.env`とAPIキーが含まれている。外部共有・ZIP化・公開リポジトリへの追加を行わない。

## 9. 中止・切り戻し条件

次のいずれかが発生したら、その段階を完了扱いにせず停止する。

- 原本側に予期しない変更が入った
- 全テストが変更前より減った、または失敗した
- mock実行がAPIへ接続しようとした
- 提出物のハッシュが変化した
- 既存Runまたはcheckpointを読み取れなくなった
- prompt、素材、生成物の探索パスが不明確になった
- 循環importや互換コードの二重実行が発生した
- 1コミット内に移動と仕様変更が混在した

切り戻しは作業版の直前コミットを基準に行い、原本から部分的に上書きして状態を混在させない。

## 10. 引き継ぎ時に伝える情報

別チャットや別担当へ引き継ぐ場合は、最低限次を伝える。

```text
- 原本: /Users/koshiro/Downloads/Agewec（変更禁止）
- 作業版: /Users/koshiro/Downloads/Agewecのコピー
- 現在の段階:
- 完了したコミット:
- 全テスト結果:
- mock実行結果:
- 未解決事項:
- 次に許可されている作業:
- Runway API実行: 禁止（個別承認がある場合のみ）
```

## 11. 進捗チェックリスト

- [x] 第0段階: ベースライン固定（既知の依存漏れはベースライン文書に記録）
- [x] 第1段階: 旧版隔離とパッケージ設定修正（240テスト・CLI・wheel検証済み）
- [x] 第2段階: 分割前の契約とパス管理（246テスト・mock完走・差し戻し4経路検証済み）
- [x] 第3段階: `pipeline_runtime.py`分割（249テスト・mock完走・差し戻し4経路検証済み）
- [x] 第4段階: LLM・決定論ノード分割（253テスト・mock完走・差し戻し4経路検証済み）
- [x] 第5段階: `runtime/`集約（260テスト・mock完走・新旧checkpoint互換検証済み）
- [x] 第6段階: 正式ディレクトリへ移設（261テスト・新旧CLI・wheel・mock・新旧resume検証済み）
- [ ] 第7段階: 提出物・文書整理
- [ ] 第8段階: 最終検証

---

この文書は実装許可ではない。各段階の開始前に、利用者からその段階について明示的な実装許可を得ること。

## 12. 第2段階の完了記録

- パス解決を`agewec_v2.paths.RuntimePaths`に集約
- 相対パスの基準ディレクトリをテストで固定
- 全Phaseの入力・出力・副作用を`phase_contracts.py`に宣言
- `target_cut_id`は、全カット分のRequestがある場合だけ差分更新に使用
- 同一カットの直接再生成以外では、旧Artifactを再利用しない契約を明示
- 全246テスト成功
- mock本番CLIが30秒・1カットで完走（Runway・外部LLM未使用）
- 差し戻し`d / s / g / n`の4経路がすべて成功
- 提出済5ファイルのSHA-256が原本と一致

## 13. 第3段階の完了記録

- `pipeline_runtime.py`を3,473行から72行の互換窓口へ縮小
- 実装を`phases/`配下の8モジュールへ責務別に移動
- `nodes_runtime.py`は互換窓口を経由せず、分割後のPhase実装を直接参照
- 旧`pipeline_runtime` importは同一の関数オブジェクトを再エクスポートし、後方互換を維持
- Phase実装が互換窓口へ逆importしないことを境界テストで固定
- 全249テスト成功
- mock本番CLIが30秒・1カットで完走（Runway・外部LLM未使用）
- 差し戻し`d / s / g / n`の4経路がすべて成功
- 互換窓口は第6段階まで維持し、第8段階で削除可否を判断

## 14. 第4段階の完了記録

- `nodes_llm.py`を1,852行から65行の互換窓口へ縮小
- `nodes.py`を942行から36行の互換窓口へ縮小
- LLM役割処理を`roles/`配下の6責務モジュールへ分離
- 決定的フォールバックを`fallbacks/`配下の5責務モジュールへ分離
- `nodes_runtime.py`は互換窓口を経由せず、分割後の役割・Phase実装を直接参照
- `roles/`と`fallbacks/`が互換窓口へ逆importしないことを境界テストで固定
- 旧`nodes_llm`・`nodes` importは同一の関数オブジェクトを再エクスポートし、後方互換を維持
- structured validation、LLM失敗時のfallback、人間フィードバック経路を関連テストで確認
- 全253テスト成功
- mock本番CLIが30秒・1カットで完走し、動画・レポート・証跡を生成（Runway・外部LLM未使用）
- 差し戻し`d / s / g / n`の4経路がすべて成功
- 互換窓口は第6段階まで維持し、第8段階で削除可否を判断

## 15. 第5段階の完了記録

- 新規実行の作業成果物を`runtime/runs/<run_id>/`へ集約
- 新規提出候補を`runtime/submissions/<run_id>/`へ集約
- 新checkpointを`runtime/checkpoints/checkpoints.sqlite`へ分離
- `runtime/`全体をGit追跡外に設定
- `RuntimePaths`にruntime root基準と旧workflow root基準の互換解決を実装
- 新checkpointを優先し、存在しないrunは旧checkpointから自動検出
- 完了済み旧run`run-9797e26e0c`を無再実行で読み取り確認
- 未完了旧runは移行前絶対パスの誤用を防ぐため安全に停止
- 新runtimeの30秒mock本番CLIが完走し、動画・レポート・証跡を生成
- 新runtimeのH3中断後、同じrun_idから再開・完走を確認
- レポートの相対動画参照と30秒・576×384の最終動画を確認
- 全260テスト成功（テスト生成物は一時ディレクトリへ隔離）
- 旧`workflow_v2/work`・`workflow_v2/submissions`は移動・削除せず保持
- 提出済5ファイルのSHA-256が原本と一致

## 16. 第6段階の完了記録

- 現行パッケージを`workflow_v2/agewec_v2`から`src/agewec_v2`へ機械的に移設
- tests、docs、scripts、configs、workflowsをリポジトリ直下へ統合
- `pyproject.toml`のwheel対象を`src/agewec_v2`へ変更
- config、prompt、モニターUI、Comfy workflowの探索を正式配置へ更新
- 旧`workflow_v2`入口には正本へのシンボリックリンクを置き、ソースを二重管理せず互換性を維持
- 旧`workflow_v2/work`・`workflow_v2/submissions`は過去Run参照のため未変更で保持
- 正式CLIで30秒・1カットのmock run`run-2eb45fafba`が完走
- 新runtimeの完了済みrunを同じrun_idから無再生成でresume
- 完了済み旧run`run-9797e26e0c`を旧checkpointから無再生成で読み取り
- 旧`PYTHONPATH=workflow_v2`入口からimport、CLI、全テストが成功
- wheel生成に成功し、全prompt Markdownと`monitor/ui.html`の同梱を確認
- 全261テスト成功（正式入口・旧互換入口の両方）
- 原本Git作業ツリーがcleanであることを確認
- 提出済5ファイルのSHA-256が原本・作業版・開始時記録で一致
- Runway・外部LLM APIは未使用
