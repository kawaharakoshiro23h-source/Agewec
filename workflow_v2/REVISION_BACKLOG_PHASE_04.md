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
