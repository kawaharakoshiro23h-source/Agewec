from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import BaseModel

from agewec_v2.llm.config import LLMSettings
from agewec_v2.llm.cost_guard import (
    LLMBudgetExceededError,
    LLMCostGuard,
)
from agewec_v2.llm.provider import OpenAICompatibleProvider


class _Output(BaseModel):
    ok: bool


class LLMCostGuardTest(unittest.TestCase):
    def test_cumulative_ledger_blocks_request_before_limit_overrun(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            guard = LLMCostGuard(
                ledger_path=path,
                limit_usd=0.001,
                pricing_model="test-model",
                input_cost_per_million_usd=1.0,
                output_cost_per_million_usd=1.0,
            )
            reservation = guard.reserve(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
                max_output_tokens=10,
            )
            result = guard.settle(
                reservation,
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 100,
                },
                status="success",
            )
            self.assertAlmostEqual(
                result["cumulative_cost_usd"],
                0.0002,
            )
            with self.assertRaises(LLMBudgetExceededError):
                guard.reserve(
                    model="test-model",
                    system_prompt="system",
                    user_prompt="user",
                    max_output_tokens=1000,
                )

            ledger = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["requests"]), 1)
            self.assertEqual(ledger["reserved_usd"], 0.0)

    def test_openai_provider_records_actual_usage_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.json"
            env = {
                "AGEWEC_LLM_ENABLED": "true",
                "AGEWEC_LLM_PROVIDER": "openai",
                "AGEWEC_LLM_BASE_URL": "https://api.openai.com/v1",
                "AGEWEC_LLM_API_KEY": "test-secret",
                "AGEWEC_LLM_MODEL": "gpt-4o-mini",
                "AGEWEC_LLM_STRUCTURED_OUTPUT_MODE": "prompt",
                "AGEWEC_LLM_TOKEN_PARAMETER": "max_completion_tokens",
                "AGEWEC_LLM_COST_GUARD_ENABLED": "true",
                "AGEWEC_LLM_COST_LIMIT_USD": "5",
                "AGEWEC_LLM_COST_LEDGER_PATH": str(ledger_path),
                "AGEWEC_LLM_PRICING_MODEL": "gpt-4o-mini",
                "AGEWEC_LLM_INPUT_COST_PER_MILLION_USD": "0.15",
                "AGEWEC_LLM_OUTPUT_COST_PER_MILLION_USD": "0.60",
            }
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "id": "request-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "content": '{"ok": true}',
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                },
            }
            response.headers = {}

            with (
                patch.dict(os.environ, env, clear=True),
                patch(
                    "agewec_v2.llm.provider.httpx.post",
                    return_value=response,
                ),
            ):
                settings = LLMSettings.from_sources({"llm": {}})
                output = OpenAICompatibleProvider(settings).generate(
                    system_prompt="system",
                    user_prompt="user",
                    output_schema=_Output,
                    temperature=0.1,
                    max_tokens=1000,
                )

            cost = output.usage["cost_guard"]
            self.assertAlmostEqual(
                cost["request_cost_usd"],
                0.00045,
            )
            self.assertAlmostEqual(
                cost["remaining_budget_usd"],
                4.99955,
            )
            self.assertTrue(ledger_path.exists())


if __name__ == "__main__":
    unittest.main()
