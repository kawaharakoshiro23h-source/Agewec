# Phase 09: Review Board

## 状態

`implemented`

## 目的

完成動画と上流のEvidenceを提出前に総合評価する。ただし締切優先で、
AI Review Boardを使わず人間がH3で直接判断する構成も選べる。

## モード

```yaml
review_board:
  mode: human_only
```

- `human_only`: AI審査をスキップし、理由と時刻を証跡へ保存してH3へ進む
- `ai`: 共通LLM ProviderでRubric審査を実行する

`review_policies.review_board`は、AI審査後のReview Gateを制御する設定であり、
審査自体の有無は`review_board.mode`で制御する。

## AIモード

Project Brief、Concept、Storyboard、Asset Manifest、QA、Post Production結果を
役割プロンプトへ渡す。最終Technical QAが`pass`でない場合、LLMの回答に
かかわらず`revise`へ固定する。

## H3

```yaml
final_submission:
  require_human: true
```

この設定は`--auto`より優先される。H3では最終動画パス、Technical QA、
Review Board結果、未解決Warningを表示し、次を選ぶ。

- 承認してPhase 10へ進む
- フィードバック付きでPhase 08を再実行
- 中止
