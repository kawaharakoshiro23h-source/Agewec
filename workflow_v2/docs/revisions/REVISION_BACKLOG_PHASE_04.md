# Phase 04 Revision Backlog

全フェーズの仕様確認後に、Asset Curator関連の次の変更をまとめて実装する。

## 全カットへ最低1件の素材を必須化

状態: `pending`

Asset Curatorでは各Storyboardカットに1件以上の素材を必須とする。
複数素材は許可するが、素材0件のカットが1つでもある場合は
Directorへ進ませず、再選定または人間確認へ送る。

実装項目:

- 全StoryboardカットIDと選定済みカットIDを照合する
- `unassigned_cut_ids`が空でなければ`error`にする
- 1カットに複数素材を許可する
- 複数素材へ`primary / alternative`と`rank`を付ける
- Directorは`primary`を使用し、代替素材を差し替え用に保持する
- 人間の修正指示で特定カットの素材を再選定できるようにする

## AGEWEC公式素材の権利チェック省略

状態: `pending`

AGEWEC公式素材カタログ内の画像は、AGEWEC提出用途では使用可能という
前提で自動通過させる。

実装項目:

- `rights_risk`をLLM出力Schemaから削除する
- `rights_status: review_required`を削除する
- `rights_check_required: false`へ変更する
- 権利確認警告を削除する
- `usage_scope: agewec_submission`を記録する
- 元画像URLと詳細ページURLはProvenanceへ残す
- 外部素材にはこの自動通過ルールを適用しない

## 関連する既存バックログ

以下のタスクと同時に設計・実装する。

- 全公式写真のローカル保存
- 不足素材の自動ダウンロード
- 昼・夕方・夜の素材属性追加
- 夜景賞と素材フィルタの分離

## 修正2: 素材Shortlistの微修正

状態: `shortlist_implemented / follow_up_required`

285件をLLMへ直接渡さず、カット別上位候補へ絞り込む処理は実装済み。
ただし、昼夜事故をコード上で完全に防止するには次を追加する。

### 必須修正

1. **`eligible_cut_ids`を選定時に強制する**

   現在はLLMへ優先利用を指示しているだけで、ユニオンに含まれる候補なら
   対象外カットにも選択できる。選択結果の検証時に、`cut_id`が候補の
   `eligible_cut_ids`に含まれない場合はエラーとして再選定させる。

2. **`time_of_day`の表記を正規化する**

   Storyboardが使用する次の値をShortlistの判定値へ変換する。

   ```text
   morning / afternoon / late_afternoon -> day
   sunset / golden_hour / blue_hour / twilight -> dusk
   evening / nighttime -> night
   ```

   現在の`day / dusk / night`以外を`unspecified`相当に扱うと、
   `time_of_day`最優先ルールが中間カットで機能しない。

### 追加確認

- ローカル候補が0件なら、LLMを呼ぶ前に明示的なBlocking Issueにする
- Deterministic経路にも同じShortlistまたは同等の安全規則を適用する
- 昼カットへ夜候補を選べないこと、夕景表記の正規化、候補枯渇をUnit Test化する

賞ジャンル変更による直接的な夜景偏重は基本的に発生しないが、上流の
Storyboardが決めた`time_of_day`を経由した構成変化は意図した挙動として残す。
