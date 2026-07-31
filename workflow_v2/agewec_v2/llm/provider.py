"""OpenAI-compatible Chat Completions provider."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from .config import LLMSettings
from .cost_guard import CostReservation, LLMCostGuard


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    usage: dict[str, Any]
    request_id: str | None


class OpenAICompatibleProvider:
    """Common provider for LM Studio, OpenAI, and compatible cloud APIs."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.cost_guard = (
            LLMCostGuard(
                ledger_path=settings.cost_ledger_path,
                limit_usd=settings.cost_limit_usd,
                pricing_model=settings.pricing_model,
                input_cost_per_million_usd=(
                    settings.input_cost_per_million_usd
                ),
                output_cost_per_million_usd=(
                    settings.output_cost_per_million_usd
                ),
            )
            if settings.provider == "openai"
            and settings.cost_guard_enabled
            else None
        )

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            self.settings.token_parameter: max_tokens,
        }
        mode = self.settings.structured_output_mode
        if mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "strict": True,
                    "schema": output_schema.model_json_schema(),
                },
            }

        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        reservation: CostReservation | None = None
        if self.cost_guard:
            reservation = self.cost_guard.reserve(
                model=self.settings.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_output_tokens=max_tokens,
            )
        try:
            response = httpx.post(
                f"{self.settings.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            usage = dict(body.get("usage") or {})
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:2000]
            if self.cost_guard and reservation:
                self.cost_guard.settle(
                    reservation,
                    usage=None,
                    status=f"http_{response.status_code}_unknown_charge",
                )
            raise RuntimeError(
                "LLM chat/completions rejected the request: "
                f"HTTP {response.status_code}: {detail or '(empty response)'}"
            ) from exc
        except Exception:
            if self.cost_guard and reservation:
                self.cost_guard.settle(
                    reservation,
                    usage=None,
                    status="request_failed_unknown_charge",
                )
            raise
        if self.cost_guard and reservation:
            cost = self.cost_guard.settle(
                reservation,
                usage=usage,
                status="success",
            )
            usage["cost_guard"] = cost
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in text
            )
        return ProviderResponse(
            text=str(text),
            model=str(body.get("model") or self.settings.model),
            usage=usage,
            request_id=response.headers.get("x-request-id")
            or body.get("id"),
        )
