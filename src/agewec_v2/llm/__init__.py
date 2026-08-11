"""Provider-independent LLM integration for the AGEWEC pipeline."""

from .config import LLMSettings
from .provider import OpenAICompatibleProvider
from .role_runner import RoleRunResult, RoleRunner

__all__ = [
    "LLMSettings",
    "OpenAICompatibleProvider",
    "RoleRunResult",
    "RoleRunner",
]
