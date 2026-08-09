# Phase 05.5–10 Implementation Summary

## 実装された経路

```mermaid
flowchart TD
    D["05 Director"] --> S["05.5 Support Video Creator"]
    S --> H2{"H2"}
    H2 --> P["06 1カット生成"]
    P --> CQ["07A Cut QA"]
    CQ -->|"pass・残りあり"| P
    CQ -->|"生成エラー"| P
    CQ -->|"生成設定"| S
    CQ -->|"演出"| D
    CQ -->|"素材"| A["04 Asset Curator"]
    CQ -->|"全カットpass"| SQ["07B Sequence QA"]
    SQ --> POST["08 FFmpeg Post Production"]
    POST --> R{"09 AI / Human-only Review"}
    R --> H3{"H3 人間の最終提出承認"}
    H3 --> PROV["10 Provenance Package"]
```

## コード

- `pipeline_runtime.py`: Phase 05.5–10の処理
- `media_tools.py`: FFmpeg / FFprobe
- `graph_safe.py`: カット単位の循環と差し戻し
- `review.py`: 共通Review GateとH3強制確認
- `state.py`: Queue、カット結果、QA、提出成果物のState

## 実行モード

- 開発確認: `config.yaml`の`production.backend: mock`
- LLM + ComfyUI: `config_llm.yaml`
- AI Review Boardなし: `review_board.mode: human_only`
- AI Review Boardあり: `review_board.mode: ai`

## 残る外部接続

コード上の工程は完成しているが、次はモデルや素材の準備が別途必要。

- 全カットのローカル元画像
- 実ComfyUIでの全カット生成
- 映像内容を判定するVLM Provider
- 字幕、ナレーション、ACE-Step BGMを使う場合の実音声ファイル

