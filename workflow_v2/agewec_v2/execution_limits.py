"""Execution guards preventing unbounded autonomous workflow loops."""
from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.types import interrupt

from .state_safe import SafeWorkflowState


def _limit_config(state: SafeWorkflowState) -> dict[str, Any]:
    return state.get("config", {}).get("execution_limits", {})


def _max_attempts(state: SafeWorkflowState, phase: str) -> int:
    limits = _limit_config(state)
    default_retries = int(limits.get("max_retries_per_phase", 2))
    overrides = limits.get("phase_retry_overrides", {})
    retries = int(overrides.get(phase, default_retries))
    return max(1, retries + 1)


def _violations(
    state: SafeWorkflowState,
    phase: str,
    *,
    started_at: float,
    transition_count: int,
) -> list[str]:
    limits = _limit_config(state)
    violations = []
    attempts = int(state.get("attempts", {}).get(phase, 0))
    max_attempts = _max_attempts(state, phase)
    if attempts >= max_attempts:
        violations.append(
            f"{phase}: 最大実行回数{max_attempts}回に到達"
        )

    max_total = int(limits.get("max_total_phase_executions", 30))
    if transition_count >= max_total:
        violations.append(
            f"ワークフロー全体の最大フェーズ実行数{max_total}回に到達"
        )

    max_minutes = float(limits.get("max_runtime_minutes", 60))
    elapsed = max(0.0, time.time() - started_at)
    if elapsed >= max_minutes * 60:
        violations.append(
            f"最大実行時間{max_minutes:g}分に到達"
        )
    return violations


def make_execution_guard(
    phase: str,
) -> Callable[[SafeWorkflowState], dict[str, Any]]:
    """Create a guard that runs immediately before each phase execution."""

    def guard(state: SafeWorkflowState) -> dict[str, Any]:
        started_at = float(state.get("started_at") or time.time())
        count = int(state.get("transition_count", 0))
        allowances = dict(state.get("limit_allowances", {}))
        allowance = int(allowances.get(phase, 0))
        violations = _violations(
            state,
            phase,
            started_at=started_at,
            transition_count=count,
        )

        if violations and allowance > 0:
            allowances[phase] = allowance - 1
            violations = []

        if not violations:
            events = list(state.get("events", []))
            events.append(
                {
                    "t": round(time.time(), 3),
                    "type": "execution_guard",
                    "phase": phase,
                    "action": "allow",
                    "phase_execution_number": (
                        int(state.get("attempts", {}).get(phase, 0)) + 1
                    ),
                    "total_phase_executions": count + 1,
                }
            )
            return {
                "started_at": started_at,
                "transition_count": count + 1,
                "pending_phase": phase,
                "guard_route": "allow",
                "limit_status": {},
                "limit_allowances": allowances,
                "events": events,
            }

        status = {
            "phase": phase,
            "violations": violations,
            "attempts": int(state.get("attempts", {}).get(phase, 0)),
            "max_attempts": _max_attempts(state, phase),
            "total_phase_executions": count,
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
        on_limit = _limit_config(state).get("on_limit", "human_review")
        route = "abort" if on_limit == "abort" else "escalate"
        events = list(state.get("events", []))
        events.append(
            {
                "t": round(time.time(), 3),
                "type": "execution_limit",
                "phase": phase,
                "action": route,
                "violations": violations,
            }
        )
        return {
            "started_at": started_at,
            "pending_phase": phase,
            "guard_route": route,
            "limit_status": status,
            "events": events,
            "aborted": route == "abort",
        }

    guard.__name__ = f"guard_{phase}"
    return guard


def guard_router(state: SafeWorkflowState) -> str:
    return state.get("guard_route", "abort")


def execution_limit_escalation(
    state: SafeWorkflowState,
) -> dict[str, Any]:
    status = state.get("limit_status", {})
    decision = interrupt(
        {
            "kind": "execution_limit",
            "phase": status.get("phase"),
            "label": "自律実行の安全上限",
            "summary": "自動ループの上限に到達しました。",
            "violations": status.get("violations", []),
            "attempts": status.get("attempts"),
            "max_attempts": status.get("max_attempts"),
            "total_phase_executions": status.get("total_phase_executions"),
            "elapsed_seconds": status.get("elapsed_seconds"),
            "actions": ["continue_once", "abort"],
            "instruction": (
                "一度だけ継続する場合はretry、終了する場合はabortを選択"
            ),
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    continue_once = action in {
        "continue_once",
        "retry",
        "retry_with_feedback",
    }
    phase = str(status.get("phase") or state.get("pending_phase") or "")
    allowances = dict(state.get("limit_allowances", {}))
    if continue_once and phase:
        allowances[phase] = allowances.get(phase, 0) + 1
        route = phase
    else:
        route = "abort"
    events = list(state.get("events", []))
    events.append(
        {
            "t": round(time.time(), 3),
            "type": "execution_limit_decision",
            "phase": phase,
            "action": "continue_once" if continue_once else "abort",
        }
    )
    return {
        "guard_route": route,
        "limit_allowances": allowances,
        "events": events,
        "aborted": not continue_once,
    }


def escalation_router(state: SafeWorkflowState) -> str:
    return state.get("guard_route", "abort")
