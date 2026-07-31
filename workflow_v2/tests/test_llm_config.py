from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agewec_v2.llm.config import LLMSettings


class LLMConfigTest(unittest.TestCase):
    def test_lmstudio_and_openai_share_contract(self) -> None:
        base_config = {"llm": {"enabled": True, "model": "configured-model"}}
        with patch.dict(
            os.environ,
            {
                "AGEWEC_LLM_PROVIDER": "lmstudio",
                "AGEWEC_LLM_BASE_URL": "http://127.0.0.1:1234/v1",
                "AGEWEC_LLM_MODEL": "local-model",
                "AGEWEC_LLM_API_KEY": "lm-studio",
                "AGEWEC_LLM_TOKEN_PARAMETER": "max_tokens",
            },
            clear=False,
        ):
            local = LLMSettings.from_sources(base_config)
        self.assertEqual(local.provider, "lmstudio")
        self.assertEqual(local.base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual(local.api_key, "lm-studio")
        self.assertEqual(local.token_parameter, "max_tokens")

        with patch.dict(
            os.environ,
            {
                "AGEWEC_LLM_PROVIDER": "openai",
                "AGEWEC_LLM_BASE_URL": "https://api.openai.com/v1",
                "AGEWEC_LLM_API_KEY": "test-key",
                "AGEWEC_LLM_MODEL": "gpt-4o-mini",
                "AGEWEC_LLM_TOKEN_PARAMETER": "max_completion_tokens",
                "AGEWEC_LLM_PRICING_MODEL": "gpt-4o-mini",
            },
            clear=False,
        ):
            cloud = LLMSettings.from_sources(base_config)
        self.assertEqual(cloud.provider, "openai")
        self.assertEqual(cloud.base_url, "https://api.openai.com/v1")
        self.assertEqual(cloud.api_key, "test-key")
        self.assertEqual(cloud.token_parameter, "max_completion_tokens")
