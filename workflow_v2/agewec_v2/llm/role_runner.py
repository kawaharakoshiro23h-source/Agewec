"""Role prompt composition, JSON extraction, validation, and repair retries."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .config import LLMSettings, WORKFLOW_ROOT
from .provider import OpenAICompatibleProvider, ProviderResponse
from .schemas import ROLE_SCHEMAS


PROMPT_ROOT = WORKFLOW_ROOT / "agewec_v2" / "prompts"

# --- 出力言語ポリシー（全ロール共通）-----------------------------------------
# 既定は日本語。ただし2種類の例外がある。
#
#   1. 動画生成モデルへ渡す文字列（positive_prompt / negative_prompt）
#      Runway/Veo/Seedance は英語プロンプトで最も安定するため英語で書かせる。
#   2. 機械語彙（asset_id, enum値, time_of_day など）
#      下流のコードが値そのもので分岐・照合するため、翻訳されると壊れる。
#      例: visual_role は素材スコアリングとナレーション補完で英語比較される。
#      （time_of_day は normalize_time_of_day が日本語も吸収するが、
#        表記ゆれを増やさないため英語に固定する）

# 動画生成モデルへ送るため英語で書かせるフィールド
ENGLISH_PROMPT_FIELDS = ("positive_prompt", "negative_prompt")

# 翻訳せずそのまま返させる機械語彙フィールド
MACHINE_TOKEN_FIELDS = (
    "asset_id",
    "media_requirement",
    "motion_intensity",
    "time_of_day",
    "visual_role",
    "verdict",
    "route",
    "energy_curve",
    "stability",
)

LANGUAGE_POLICY = (
    "LANGUAGE POLICY (applies to every field you return):\n"
    "1. Write all human-readable prose in Japanese (日本語). This includes "
    "titles, loglines, scenes, narration, reasons, rationales, constraints, "
    "success criteria, issues, recommendations, and any other explanatory "
    "text. Do not write these in English.\n"
    "2. EXCEPTION - write these fields in English, because they are sent "
    "directly to the video generation model, which performs best in "
    "English: " + ", ".join(ENGLISH_PROMPT_FIELDS) + ".\n"
    "3. EXCEPTION - return these fields as machine tokens exactly as "
    "specified by the schema and upstream context. Never translate or "
    "localise them: " + ", ".join(MACHINE_TOKEN_FIELDS) + ", plus every "
    "value constrained by an enum in the schema.\n"
    "Numeric and identifier fields (ids, seconds, scores) are unaffected.\n"
)


@dataclass(frozen=True)
class RoleRunResult:
    output: BaseModel
    metadata: dict[str, Any]


class RoleRunner:
    def __init__(
        self,
        workflow_config: dict[str, Any],
        *,
        provider: OpenAICompatibleProvider | None = None,
    ) -> None:
        self.workflow_config = workflow_config
        self.settings = LLMSettings.from_sources(workflow_config)
        self.provider = provider or OpenAICompatibleProvider(self.settings)

    def _profile(self, role: str) -> dict[str, Any]:
        llm = self.workflow_config.get("llm", {})
        profile_name = llm.get("role_profiles", {}).get(role, "planning")
        profiles = llm.get("profiles", {})
        return dict(profiles.get(profile_name, {}))

    @staticmethod
    def _extract_json(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            chunks = stripped.split("```")
            if len(chunks) >= 3:
                stripped = chunks[1]
                if stripped.lstrip().startswith("json"):
                    stripped = stripped.lstrip()[4:].lstrip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            object_start = stripped.find("{")
            object_end = stripped.rfind("}")
            if object_start >= 0 and object_end > object_start:
                return json.loads(stripped[object_start : object_end + 1])
            array_start = stripped.find("[")
            array_end = stripped.rfind("]")
            if array_start >= 0 and array_end > array_start:
                return json.loads(stripped[array_start : array_end + 1])
            raise

    def _system_prompt(self, role: str, schema: type[BaseModel]) -> str:
        prompt_path = PROMPT_ROOT / f"{role}.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Role prompt not found: {prompt_path}")
        role_prompt = prompt_path.read_text(encoding="utf-8").strip()
        schema_json = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            indent=2,
        )
        return (
            f"{role_prompt}\n\n"
            f"{LANGUAGE_POLICY}\n"
            "If review_feedback in the user JSON is non-empty, treat it as a "
            "mandatory revision instruction. Reflect it in the returned "
            "artifact while preserving approved constraints that it does not "
            "explicitly change. Never claim that feedback was applied unless "
            "the returned fields actually reflect it.\n\n"
            "Return one JSON object only. Do not use Markdown fences.\n"
            f"Your output must validate against this JSON Schema:\n{schema_json}"
        )

    @staticmethod
    def _user_prompt(
        role: str,
        upstream: dict[str, Any],
        feedback: str,
        repair: dict[str, str] | None,
    ) -> str:
        payload = {
            "role": role,
            "approved_upstream_context": upstream,
            "review_feedback": feedback,
            "task": "Produce the next approved workflow artifact.",
        }
        if repair:
            payload["repair"] = repair
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def run(
        self,
        *,
        role: str,
        upstream: dict[str, Any],
        feedback: str = "",
    ) -> RoleRunResult:
        if not self.settings.enabled:
            raise RuntimeError("LLM integration is disabled")
        schema = ROLE_SCHEMAS[role]
        profile = self._profile(role)
        temperature = float(profile.get("temperature", 0.4))
        max_tokens = int(profile.get("max_tokens", 2500))
        system_prompt = self._system_prompt(role, schema)
        repair: dict[str, str] | None = None
        errors: list[str] = []
        started = time.monotonic()
        last_response: ProviderResponse | None = None

        total_attempts = self.settings.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            last_response = self.provider.generate(
                system_prompt=system_prompt,
                user_prompt=self._user_prompt(role, upstream, feedback, repair),
                output_schema=schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                raw = self._extract_json(last_response.text)
                validated = schema.model_validate(raw)
                return RoleRunResult(
                    output=validated,
                    metadata={
                        "provider": self.settings.provider,
                        "base_url": self.settings.base_url,
                        "model": last_response.model,
                        "usage": last_response.usage,
                        "request_id": last_response.request_id,
                        "attempts": attempt,
                        "elapsed_seconds": round(
                            time.monotonic() - started,
                            3,
                        ),
                        "structured_output_mode": (
                            self.settings.structured_output_mode
                        ),
                    },
                )
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                errors.append(error)
                repair = {
                    "previous_output": last_response.text,
                    "validation_error": error,
                    "instruction": (
                        "Return a corrected complete JSON object. "
                        "Do not explain the correction."
                    ),
                }

        raise ValueError(
            f"{role} failed structured validation after {total_attempts} attempts: "
            + " | ".join(errors)
        )
