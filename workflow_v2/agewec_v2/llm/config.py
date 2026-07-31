"""LLM configuration loaded from workflow config and environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKFLOW_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = WORKFLOW_ROOT.parent


def load_project_dotenv() -> None:
    """Load the project .env without overriding process environment values."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        value = os.environ.get(key)
        if value:
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_retries: int
    structured_output_mode: str
    strict_mode: bool
    token_parameter: str
    cost_guard_enabled: bool
    cost_limit_usd: float
    cost_ledger_path: Path
    pricing_model: str
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float

    @classmethod
    def from_sources(cls, workflow_config: dict[str, Any]) -> "LLMSettings":
        load_project_dotenv()
        llm = workflow_config.get("llm", {})
        provider = _env(
            "AGEWEC_LLM_PROVIDER",
            default=str(llm.get("provider", "lmstudio")),
        ).lower()

        if provider == "openai":
            default_base = "https://api.openai.com/v1"
            default_token_parameter = "max_completion_tokens"
        else:
            default_base = "http://127.0.0.1:1234/v1"
            default_token_parameter = "max_tokens"

        base_url = _env(
            "AGEWEC_LLM_BASE_URL",
            "LOCAL_BASE_URL",
            "OPENAI_BASE_URL",
            default=str(llm.get("base_url", default_base)),
        ).rstrip("/")
        api_key = _env(
            "AGEWEC_LLM_API_KEY",
            "LOCAL_API_KEY",
            "OPENAI_API_KEY",
            default=str(llm.get("api_key", "")),
        )
        if provider in {"lmstudio", "local"} and not api_key:
            api_key = "lm-studio"

        model = _env(
            "AGEWEC_LLM_MODEL",
            "LLM_MODEL",
            default=str(llm.get("model", "")),
        )
        # An explicitly safe config (`llm.enabled: false`) must remain local
        # even when the project .env contains cloud credentials.  Other configs
        # may still be disabled or enabled through the environment.
        enabled = (
            False
            if llm.get("enabled") is False
            else _env_bool(
                "AGEWEC_LLM_ENABLED",
                bool(llm.get("enabled", False)),
            )
        )
        cost_guard = llm.get("cost_guard", {})
        ledger_value = _env(
            "AGEWEC_LLM_COST_LEDGER_PATH",
            default=str(
                cost_guard.get(
                    "ledger_path",
                    "work/llm_cost_ledger.json",
                )
            ),
        )
        ledger_path = Path(ledger_value).expanduser()
        if not ledger_path.is_absolute():
            ledger_path = WORKFLOW_ROOT / ledger_path
        settings = cls(
            enabled=enabled,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=float(
                _env(
                    "AGEWEC_LLM_TIMEOUT_SECONDS",
                    default=str(llm.get("timeout_seconds", 120)),
                )
            ),
            max_retries=int(
                _env(
                    "AGEWEC_LLM_MAX_RETRIES",
                    default=str(llm.get("max_retries", 2)),
                )
            ),
            structured_output_mode=_env(
                "AGEWEC_LLM_STRUCTURED_OUTPUT_MODE",
                default=str(llm.get("structured_output_mode", "prompt")),
            ),
            strict_mode=_env_bool(
                "AGEWEC_LLM_STRICT_MODE",
                bool(llm.get("strict_mode", True)),
            ),
            token_parameter=_env(
                "AGEWEC_LLM_TOKEN_PARAMETER",
                default=str(llm.get("token_parameter", default_token_parameter)),
            ),
            cost_guard_enabled=_env_bool(
                "AGEWEC_LLM_COST_GUARD_ENABLED",
                bool(cost_guard.get("enabled", True)),
            ),
            cost_limit_usd=float(
                _env(
                    "AGEWEC_LLM_COST_LIMIT_USD",
                    default=str(cost_guard.get("limit_usd", 5.0)),
                )
            ),
            cost_ledger_path=ledger_path,
            pricing_model=_env(
                "AGEWEC_LLM_PRICING_MODEL",
                default=str(cost_guard.get("pricing_model", "gpt-4o-mini")),
            ),
            input_cost_per_million_usd=float(
                _env(
                    "AGEWEC_LLM_INPUT_COST_PER_MILLION_USD",
                    default=str(
                        cost_guard.get(
                            "input_cost_per_million_usd",
                            0.15,
                        )
                    ),
                )
            ),
            output_cost_per_million_usd=float(
                _env(
                    "AGEWEC_LLM_OUTPUT_COST_PER_MILLION_USD",
                    default=str(
                        cost_guard.get(
                            "output_cost_per_million_usd",
                            0.60,
                        )
                    ),
                )
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.model:
            raise ValueError(
                "LLM is enabled but no model is configured. "
                "Set AGEWEC_LLM_MODEL or llm.model."
            )
        if self.provider not in {
            "lmstudio",
            "local",
            "openai",
            "openai_compatible",
        }:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")
        if self.provider in {"openai", "openai_compatible"} and not self.api_key:
            raise ValueError(
                "Cloud LLM is enabled but AGEWEC_LLM_API_KEY is empty."
            )
        if self.structured_output_mode not in {
            "prompt",
            "json_object",
            "json_schema",
        }:
            raise ValueError(
                "structured_output_mode must be prompt, json_object, or json_schema"
            )
        if self.token_parameter not in {
            "max_tokens",
            "max_completion_tokens",
        }:
            raise ValueError(
                "token_parameter must be max_tokens or max_completion_tokens"
            )
        if self.provider == "openai" and self.cost_guard_enabled:
            if self.cost_limit_usd <= 0:
                raise ValueError("LLM cost limit must be greater than zero")
            if (
                self.input_cost_per_million_usd < 0
                or self.output_cost_per_million_usd < 0
            ):
                raise ValueError("LLM token prices cannot be negative")
            if not self.pricing_model:
                raise ValueError("LLM pricing_model is required")
            if not (
                self.model == self.pricing_model
                or self.model.startswith(f"{self.pricing_model}-")
            ):
                raise ValueError(
                    "Cost guard pricing does not match the active model: "
                    f"model={self.model}, pricing_model={self.pricing_model}. "
                    "Configure matching token prices before using this model."
                )
