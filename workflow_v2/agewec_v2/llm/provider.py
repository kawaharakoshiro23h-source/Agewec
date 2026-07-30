"""OpenAI-compatible Chat Completions provider."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from .config import LLMSettings


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
        response = httpx.post(
            f"{self.settings.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.settings.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "LLM response does not contain choices[0].message.content"
            ) from exc
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in text
            )
        return ProviderResponse(
            text=str(text),
            model=str(body.get("model") or self.settings.model),
            usage=dict(body.get("usage") or {}),
            request_id=response.headers.get("x-request-id")
            or body.get("id"),
        )
