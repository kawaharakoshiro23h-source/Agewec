"""Provider-independent LLM integration for workflow_v2."""

from .config import LLMSettings
from .provider import OpenAICompatibleProvider
from .role_runner import RoleRunResult, RoleRunner

__all__ = [
    "LLMSettings",
    "OpenAICompatibleProvider",
    "RoleRunResult",
    "RoleRunner",
]
