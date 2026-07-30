"""Role prompt composition, JSON extraction, validation, and repair retries."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .config import LLMSettings, WORKFLOW_ROOT
from .provider import OpenAICompatibleProvider, ProviderResponse
from .schemas import ROLE_SCHEMAS


PROMPT_ROOT = WORKFLOW_ROOT / "agewec_v2" / "prompts"


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
