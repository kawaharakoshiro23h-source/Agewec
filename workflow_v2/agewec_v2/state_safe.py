"""Extended state used by the guarded workflow graph."""
from __future__ import annotations

from typing import Any

from .state import WorkflowState


class SafeWorkflowState(WorkflowState, total=False):
    started_at: float
    transition_count: int
    pending_phase: str
    guard_route: str
    limit_status: dict[str, Any]
    limit_allowances: dict[str, int]
