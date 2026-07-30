# Phase 05 Revision Backlog

全フェーズの仕様確認後に、Creative DirectorとDirectorの責務を次の
2層構造へ変更する。

## カメラ設計を全体意図と個別提案へ分離

状態: `pending`

### Creative Director

具体的なカメラワーク一覧や許可リストは固定しない。
作品全体で何を感じさせるか、どのように運動量を変化させるか、
守るべき境界だけを`camera_intent`として定義する。

目標Schema例:

```json
{
  "camera_intent": {
    "viewer_experience": "街の一日を観察し、最後に夜景の壮大さを感じさせる",
    "energy_curve": "昼は自然、夕方から高揚し、夜景で最大化する",
    "stability": "実在する建築と地形が崩れない範囲",
    "continuity": "カット間で運動方向を不自然に変えない",
    "hard_constraints": [
      "過度な回転を避ける",
      "建築物を変形させない"
    ]
  }
}
```

カメラワークの種類を列挙したホワイトリストにはしない。
Directorが新しいムーブメントを提案できる余地を残す。

### Director

各カットに最適なカメラワークを自由に提案し、その理由と
Creative Directorの全体意図との関係を説明する。

目標Schema例:

```json
{
  "cut_id": 4,
  "camera_motion": "上空から街へゆっくり降下しながら前進する",
  "motion_intensity": "medium",
  "camera_rationale": "夜景の広がりをクライマックスとして見せるため",
  "relation_to_global_intent": "adapted",
  "deviation_reason": ""
}
```

追加予定フィールド:

- `motion_intensity`
- `camera_rationale`
- `relation_to_global_intent`
  - `aligned`
  - `adapted`
  - `deviation`
- `deviation_reason`

`relation_to_global_intent: deviation`の場合は`deviation_reason`を必須にする。

## H2の比較表示

状態: `pending`

H2では次を同時に表示する。

- Creative Directorが決めた作品全体の`camera_intent`
- 各カットの場面、秒数、素材
- Directorが提案したカメラワーク
- 動きの強さ
- そのカメラワークを選んだ理由
- 全体意図との関係
- 方針から外れる場合の理由

表示イメージ:

```text
全体意図:
昼の活動を観察し、夜景で壮大さを最大化する

Cut 1:
カメラ: 緩やかな横移動
理由: 昼の街の日常を自然に見せる
関係: aligned

Cut 4:
カメラ: 上空から街へ降下しながら前進
理由: 最終夜景のスケールを強調する
関係: adapted
```

## 自動検証

状態: `pending`

- Directorの全Shotに`camera_rationale`を必須化する
- `relation_to_global_intent`を列挙型で検証する
- `deviation`の場合は理由が空でないことを検証する
- Creative Directorの`hard_constraints`との明確な矛盾を検出する
- H2で矛盾や例外を警告として表示する

## 素材選定との連携

状態: `pending`

- Asset Curatorの`primary`素材を標準入力として使う
- `alternative`素材は再生成・差し替え候補として保持する
- Directorが別カット向けの素材を誤って使わないよう検証する
- Production開始前に`local_path`の存在を確認する

## 将来の戻り先

状態: `pending`

Directorが各カットを検討した結果、Creative Directorの全体意図自体を
変える必要がある場合に、H2からCreative Directorへ戻る経路を検討する。

単一カットの例外はDirector側で理由付き承認とし、多数のカットが
`deviation`になる場合だけ全体方針の再検討を促す。
