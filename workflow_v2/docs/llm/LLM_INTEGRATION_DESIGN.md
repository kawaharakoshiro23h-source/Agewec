# AGEWEC Workflow v2 — LLM Integration Design

## 1. Goal

LM StudioなどのローカルLLMと、クラウドLLMを同じ役割実行インターフェースで
扱う。プロバイダーを切り替えても、LangGraphノード、役割プロンプト、
構造化出力スキーマ、Review Gateを変更しない。

```text
LangGraph Role Node
        ↓
RoleRunner
  ├─ role prompt
  ├─ upstream context
  ├─ human/AI feedback
  └─ output schema
        ↓
LLMProvider
  ├─ LM Studio
  ├─ OpenAI
  └─ OpenAI-compatible cloud
        ↓
JSON parse → Pydantic validation → NodeResult
        ↓
Review Gate
```

## 2. Configuration boundary

### `.env`: credentials and deployment-specific connection values

```env
AGEWEC_LLM_PROVIDER=lmstudio
AGEWEC_LLM_BASE_URL=http://127.0.0.1:1234/v1
AGEWEC_LLM_API_KEY=lm-studio
AGEWEC_LLM_MODEL=local-model
AGEWEC_LLM_TIMEOUT_SECONDS=120
AGEWEC_LLM_MAX_RETRIES=2
```

LM StudioのAPIキーは認証用の本物の鍵ではなく、OpenAI互換クライアントへ
渡すダミー値でよい。

クラウドへ切り替える場合:

```env
AGEWEC_LLM_PROVIDER=openai
AGEWEC_LLM_BASE_URL=https://api.openai.com/v1
AGEWEC_LLM_API_KEY=<secret>
AGEWEC_LLM_MODEL=<model-id>
```

`.env` はGitへコミットしない。Provenanceにもキーを保存しない。

### `config.yaml`: workflow behavior

```yaml
llm:
  enabled: true
  provider_from_env: AGEWEC_LLM_PROVIDER
  default_model_from_env: AGEWEC_LLM_MODEL
  strict_mode: true

  profiles:
    planning:
      temperature: 0.4
      max_tokens: 2500
    writing:
      temperature: 0.7
      max_tokens: 4000
    evaluation:
      temperature: 0.1
      max_tokens: 1800

  role_profiles:
    executive_producer: planning
    creative_director: planning
    writer_storyboard: writing
    asset_curator: evaluation
    director: planning
    visual_qa: evaluation
    post_production: planning
    review_board: evaluation
```

`strict_mode: true` では接続・JSON・検証エラーをモックへ隠さず、
`NodeResult.status=error` としてReview Gateへ渡す。

## 3. File layout

```text
workflow_v2/
├── agewec_v2/
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── provider.py
│   │   ├── role_runner.py
│   │   └── schemas.py
│   ├── prompts/
│   │   ├── executive_producer.md
│   │   ├── creative_director.md
│   │   ├── writer_storyboard.md
│   │   ├── asset_curator.md
│   │   ├── director.md
│   │   ├── visual_qa.md
│   │   ├── post_production.md
│   │   └── review_board.md
│   └── nodes.py
└── config.yaml
```

## 4. Provider contract

```python
class LLMProvider(Protocol):
    def generate_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict,
        output_schema: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> BaseModel: ...
```

最初は `OpenAICompatibleProvider` を一つ実装する。LM StudioとOpenAI互換
クラウドの違いは `base_url / api_key / model` だけに閉じ込める。

将来、互換APIではないサービスが必要になった場合だけProvider実装を追加する。

## 5. RoleRunner contract

すべてのLLM役割ノードは直接APIを呼ばず、RoleRunnerを経由する。

```python
result = role_runner.run(
    role="creative_director",
    upstream={
        "project": state["project"],
        "executive_producer": state["phase_results"]["executive_producer"],
    },
    feedback=state["feedback"].get("creative_director", ""),
)
```

RoleRunnerの責務:

1. 役割プロンプトを読み込む
2. 上流成果物と修正フィードバックを渡す
3. Providerを呼び出す
4. JSONを抽出する
5. Pydanticスキーマで検証する
6. JSON/検証エラー時は最大回数まで修復依頼する
7. 成功またはエラーを共通NodeResultへ変換する
8. 使用モデル、所要時間、token usageを証跡へ渡す

## 6. Role and schema mapping

| Phase | LLM/VLM | Structured output | Important restriction |
|---|---|---|---|
| Executive Producer | LLM | `ProjectBrief` | 目的・制約・評価基準を確定 |
| Creative Director | LLM | `CreativeConcept` | ProjectBriefから逸脱しない |
| Writer / Storyboard | LLM | `Storyboard` | 合計秒数と各カット秒数を検証 |
| Asset Curator | LLM | `AssetSelection` | 渡されたasset ID以外を作らない |
| Director | LLM | `DirectionPlan` | 実在素材と生成設定を紐付ける |
| Image / Video Production | Tool | `MediaArtifact[]` | LLMではなくComfyUIを呼ぶ |
| Visual QA | VLM + deterministic checks | `VisualQAResult` | routeをproduction/asset/postから選択 |
| Post Production | LLM plan + Tool | `EditPlan` | FFmpeg実行自体は決定的コード |
| Review Board | LLM/VLM | `ReviewBoardResult` | rubric、根拠、pass/reviseを返す |
| Final Submission | Review Gate | `ReviewDecision` | 人間または設定済みpolicy |
| Provenance | Tool | `ProvenanceRecord` | LLMを使わず事実のみ保存 |

すべてをLLMに任せない。外部処理、ファイル検査、権利情報、証跡は
決定的なコードで扱い、LLMは判断・企画・文章化に限定する。

## 7. Core Pydantic schemas

### ProjectBrief

```text
objective
audience
deliverable
constraints[]
success_criteria[]
```

### CreativeConcept

```text
title
logline
tone[]
visual_language
audio_direction
```

### Storyboard

```text
total_seconds
cuts[]:
  id
  scene
  narration
  seconds
  media_strategy
```

Validation:

- cut IDは重複不可
- secondsは正数
- 各cutの合計とtotal_secondsが一致
- target durationの許容範囲内

### AssetSelection

```text
selections[]:
  cut_id
  asset_id
  reason
  rights_risk
missing_requirements[]
```

Validation:

- `asset_id` は入力候補に存在しなければならない
- URLをLLMに生成させない

### DirectionPlan

```text
shots[]:
  cut_id
  asset_id
  positive_prompt
  negative_prompt
  camera_motion
  generation_profile
```

### VisualQAResult

```text
verdict
route
issues[]
cut_results[]
confidence
```

`route` は次に限定:

- `image_video_production`
- `asset_curator`
- `post_production`

### ReviewBoardResult

```text
rubric_scores
average
verdict
recommendations[]
confidence
```

## 8. Prompt composition

各役割プロンプトは次の構成へ統一する。

```text
SYSTEM
- あなたの役割
- 判断範囲
- 禁止事項
- 出力スキーマ

USER
- project settings
- approved upstream artifacts
- available real assets/tools
- current budget/retry count
- previous review feedback
- exact requested task
```

上流成果物全体を無制限に渡さず、その役割に必要な情報だけを渡す。

人間から再実行指示が来た場合:

```text
Previous output:
...

Review feedback:
「ナレーションを短くし、工場夜景を中心にする」

Revise the output while preserving all approved upstream constraints.
```

## 9. Error and retry behavior

```text
API connection failure
JSON parse failure
schema validation failure
policy violation
        ↓
RoleRunner internal retry
        ↓ retry exhausted
NodeResult.status = error
        ↓
Review Gate
```

禁止事項:

- 本番実行で無条件にモックへフォールバックしない
- 不正JSONをそのままStateへ入れない
- LLMが生成したURLや権利情報を事実として保存しない
- 無制限に自動再試行しない

## 10. Autonomous mode

自律モードは単なる承認スキップではなく、次を組み合わせる。

1. deterministic validation
2. LLM/VLM evaluator
3. Review Policy
4. retry limit
5. cost/time budget
6. escalation rule

```text
phase result
   ↓
deterministic checks
   ↓
AI evaluator
   ↓
pass → next phase
revise → permitted upstream phase
uncertain / retry exhausted → human escalation
```

`never` は完全無人だが、安全性より完走を優先するモード。
通常の自律運転は `on_exception` を推奨する。

## 11. Local resource policy

MacBook Pro 32GBでは、LM StudioモデルとComfyUI動画モデルの同時常駐で
メモリ圧迫が起きる可能性がある。

初期実装は次の順次実行にする。

```text
P/C/W/A/DのLLM処理を先に完了
→ 承認済み構造化データを保存
→ Image/Video Production
→ Visual QA
```

Visual QAをローカルVLMで行う場合は、動画から代表フレームを抽出して評価する。
同時常駐が厳しければ、Visual QAだけクラウドへ切り替えられるよう、
将来は役割単位のProvider overrideを許可する。

## 12. Implementation order

1. `LLMConfig` と `.env` 読み込み
2. `OpenAICompatibleProvider`
3. Pydantic schemas
4. RoleRunnerとJSON修復再試行
5. Executive ProducerをLLM化
6. Creative Director / Writer / DirectorをLLM化
7. Asset Curatorを候補ID制約付きでLLM化
8. Visual QA / Review Boardの評価出力
9. token・時間・モデル情報をProvenanceへ追加
10. ローカル／クラウド切替テスト

最初の完了条件:

- `.env`と`config.yaml`だけでLM Studio / cloudを切り替えられる
- LangGraphやrole nodeコードは切替時に変更しない
- 全役割がPydantic検証済みの構造化データを返す
- エラーがモックで隠れずReview Gateへ届く
- APIキーがログ・State・Provenanceへ残らない
