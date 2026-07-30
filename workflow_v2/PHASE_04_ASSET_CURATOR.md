# Phase 04: Asset Curator

## 目的

Writer / Storyboardが作成した各カットについて、AGEWEC公式素材カタログから
場面に適した具体的な写真を選定する。

各カットには1件以上の素材を必須とし、必要に応じて複数候補を持てる設計を
目標とする。

AGEWEC公式素材カタログ内の画像はAGEWEC提出用途で使用可能という前提とし、
権利リスクの判定工程は省略する。ただし、出典情報は証跡として保存する。

## 修正後の確定仕様

状態: `design_confirmed / implementation_pending`

- 全カットに最低1件の素材を必須とし、0件のカットがあれば次工程へ進めない。
- 複数候補は`primary`と`alternatives`へ順位付けする。
- 修正時は対象カットだけを再選定でき、承認済みの他カットを固定する。
- AGEWEC公式素材には`usage_scope: agewec_submission`を記録し、重複する権利判定は省略する。
- 外部素材は自動採用せず、別の権利確認対象にする。
- 公式素材は事前にまとめてローカル保存し、通常実行ではローカルカタログを検索する。
- カタログには時刻帯、用途、場所、被写体、ローカルパス、取得日時、ファイルサイズ、ハッシュを保存する。
- ローカルファイル欠損時だけ、許可された公式URLから再取得するフォールバックを使う。

詳細は[Phase 04の修正案](./REVISION_BACKLOG_PHASE_04.md)を参照する。

## 処理フロー

```mermaid
flowchart LR
    G["Execution Guard"] --> S["Storyboardを取得"]
    S --> C["素材候補を抽出"]
    C --> L["Asset Curator LLM"]
    L --> V{"カットID・素材ID検証"}
    V -->|"不正"| L
    V -->|"正常"| Z{"全カットに1件以上ある?"}
    Z -->|"ない"| L
    Z -->|"ある"| D{"ローカルにある?"}
    D -->|"ある"| M["Asset Manifestを保存"]
    D -->|"ない"| DL["将来: 自動ダウンロード"]
    DL --> M
    M --> H{"人間確認"}
    H -->|"承認"| N["Directorへ"]
    H -->|"再選定"| G
    H -->|"中止"| X["終了"]
```

`全カットに1件以上あるか`の強制検証と自動ダウンロードは、現在はまだ
未実装であり、後の修正対象。

## 最初に渡す情報

### 承認済みStoryboard

Writer / Storyboardが作成した各カットの次の情報を渡す。

```json
{
  "id": 1,
  "name": "北九州の一日",
  "scene": "昼の門司港で人々が活動している様子",
  "narration": "海と山に抱かれた、動き続ける街。",
  "seconds": 6,
  "media_strategy": "still"
}
```

- `id`: 素材を対応付けるカットID
- `scene`: 必要な写真の内容
- `seconds`: 素材を使用する時間
- `media_strategy`: 静止画利用か、Image-to-Videoの入力にするか

### 素材候補

`asset_catalog.json`から候補を作成し、LLMへ次のメタデータを渡す。

```json
{
  "asset_id": "asset-001",
  "title": "皿倉山夜景03",
  "source_url": "画像URL",
  "detail_url": "素材詳細ページURL",
  "genres": [
    "イルミネーション・夜景"
  ],
  "areas": [
    "八幡東区"
  ],
  "local_path": "assets_dl/皿倉山夜景03.jpg"
}
```

LLMには画像データそのものではなく、現在はタイトル、ジャンル、地域などの
メタデータを渡して選定させる。

再実行の場合は、人間が前回入力した`review_feedback`も渡す。

## このフェーズの処理

1. Execution Guardがフェーズ実行回数、全体実行数、経過時間を確認する
2. 承認済みStoryboardを取得する
3. 素材カタログから候補を抽出する
4. Storyboardと素材候補をAsset Curator LLMへ渡す
5. LLMが各カットに適した`asset_id`と選定理由を返す
6. 存在するカットIDと素材IDだけが使われているか検証する
7. 選定結果と素材情報を結合してAsset Manifestを作成する
8. 結果とLLM実行情報をLangGraphのStateへ保存する
9. Review Gateで人間の判断を待つ

LLMは入力として渡された`asset_id`だけを選択できる。存在しない写真、
URL、タイトルを新しく作ってはならない。

LLM出力の内部修正試行は、現在は初回を含め最大3回。

## カットと素材の関係

目標とするルールは次のとおり。

- 各カットには素材を1件以上割り当てる
- 1つのカットに複数素材を割り当ててもよい
- 素材0件のカットがあれば次のDirectorへ進ませない
- 複数素材の場合はメイン素材と代替候補を区別する
- 同じ素材を複数カットで使うかどうかは制作方針で制御する

```mermaid
flowchart LR
    C1["Cut 1"] --> A1["Primary Asset"]
    C1 --> A2["Alternative Asset"]
    C2["Cut 2"] --> A3["Primary Asset"]
    C3["Cut 3"] --> A4["Primary Asset"]
    C3 --> A5["Alternative Asset"]
```

現在のコードは複数素材を選択できるが、全カットに1件以上あることを
強制していない。未割り当てカットを`unassigned_cut_ids`へ記録するだけで、
次へ進む可能性がある。

## 次のステップへ渡す情報

現在の出力は素材情報を含む`selected_assets`の一覧。

後の修正では、次のような`AssetManifest`へ整理する。

```json
{
  "catalog_source": "AGEWEC公式素材カタログ",
  "usage_scope": "agewec_submission",
  "rights_check_required": false,
  "selected_assets": [
    {
      "cut_id": 1,
      "asset_id": "asset-012",
      "role": "primary",
      "rank": 1,
      "title": "昼の門司港レトロ",
      "source_url": "画像URL",
      "detail_url": "詳細ページURL",
      "local_path": "assets_dl/mojiko-day.jpg",
      "selection_reason": "昼の活動を表すカットに適している"
    },
    {
      "cut_id": 1,
      "asset_id": "asset-013",
      "role": "alternative",
      "rank": 2,
      "title": "門司港の街並み",
      "source_url": "画像URL",
      "detail_url": "詳細ページURL",
      "local_path": "assets_dl/mojiko-street.jpg",
      "selection_reason": "代替の昼素材として利用できる"
    }
  ],
  "missing_requirements": [],
  "unassigned_cut_ids": []
}
```

Directorは各カットの`primary`素材を基本入力として使い、代替候補は人間の
変更や生成失敗時の差し替え用に保持する。

## AGEWEC公式素材の権利方針

今回のAGEWEC公式素材カタログ内の画像は、AGEWEC提出用途で使用可能という
前提にする。

そのため、後の修正では次を行う。

- LLMによる`rights_risk`分類を削除する
- `rights_status: review_required`を削除する
- `rights_check_required`を`false`にする
- 権利確認を促す警告を削除する
- 元画像URLと詳細ページURLは証跡として残す
- `usage_scope: agewec_submission`を記録する

この自動通過はAGEWEC公式素材カタログだけに適用する。将来、外部URLや
別サービスの素材を追加する場合は、同じ扱いにせず別の利用条件を持たせる。

## 人間確認

現在は`manual`モードなので、Asset Curatorの直後にあるReview Gateで
必ず停止する。

これはDirector後のH2とは別の、工程ごとのReview Gate。

### 選択できる操作

- `approve`: 素材選定を承認してDirectorへ進む
- `retry_with_feedback`: 修正指示を渡して素材選定を再実行する
- `abort`: ワークフローを終了する

### 現在、修正指示できる範囲

- 特定カットの写真を別候補へ変更
- 1カットへ複数候補を用意する
- 昼・夕方・夜のバランス
- 特定の場所や被写体を優先する
- 同じ素材の重複を避ける
- 最終カットの夜景素材を指定する
- 不足している素材を再選定する

例:

```text
Cut 1には昼の門司港素材を2候補用意してください。
Cut 4のメイン素材は皿倉山からの夜景にしてください。
すべてのカットに最低1件の素材を割り当ててください。
```

修正指示を受けると、特定素材だけを直接編集するのではなく、LLMが
素材選定全体を再生成する。

### 現在、修正指示だけでは変更できないもの

- Storyboardのカット内容
- 各カットの秒数
- CreativeConcept
- プロジェクト全体の目標尺
- ComfyUIの生成設定
- 素材の自動ダウンロード
- Review Policyや実行上限などのシステム設定

Asset CuratorのReview GateからWriterへ直接戻る経路も、現在は実装されて
いない。

## 現在の注意点

- 全カットへの最低1素材割り当ては未実装
- 複数素材の`primary / alternative`区別は未実装
- 未ダウンロード素材をLLMが選ぶ可能性がある
- 不足素材の自動ダウンロードは未実装
- 現在は夜景賞を夜景ジャンルの素材フィルタとして使っている
- 昼・夕方・夜をカット単位で検索する属性は未実装
- LLMは実画像ではなくメタデータを中心に選定している
- 公式素材向けの権利チェック省略は未実装

これらは全フェーズの仕様確認後にまとめて修正する。

## エラー時

承認済みStoryboardが存在しない場合は実行せず、結果を`error`として
Review Gateへ渡す。

存在しないカットIDや素材IDをLLMが返した場合はJSON変換を失敗させ、
LLMへ修正を依頼する。

後の修正では、素材0件のカットが1つでも存在する場合も`error`として
次のDirectorへ進ませない。
