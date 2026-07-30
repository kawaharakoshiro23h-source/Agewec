"""Reusable human/automatic Review Gate implementation."""
from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.types import interrupt

from .state import WorkflowState


PRESETS: dict[str, dict[str, str]] = {
    "manual": {"*": "always"},
    "supervised": {
        "*": "on_exception",
        "executive_producer": "always",
        "creative_director": "always",
        "director": "always",
        "image_video_production": "always",
        "review_board": "always",
        "final_submission": "always",
        "provenance": "always",
    },
    "autonomous": {
        "*": "on_exception",
        "final_submission": "always",
    },
}

VALID_POLICIES = {"always", "on_exception", "never"}
VALID_ACTIONS = {"approve", "retry_with_feedback", "abort"}


def resolve_policy(config: dict[str, Any], phase: str) -> str:
    preset_name = config.get("autonomy_preset", "manual")
    preset = PRESETS.get(preset_name, {})
    policy = preset.get(phase, preset.get("*", "always"))
    overrides = config.get("review_policies", {})
    if preset_name == "custom":
        policy = overrides.get(phase, policy)
    if policy not in VALID_POLICIES:
        raise ValueError(f"Unknown review policy for {phase}: {policy}")
    return policy


def _has_exception(
    result: dict[str, Any],
    confidence_threshold: float,
) -> bool:
    if result.get("status") not in {"success", "approved"}:
        return True
    if result.get("blocking_issues"):
        return True
    confidence = result.get("confidence")
    return confidence is not None and float(confidence) < confidence_threshold


def _normalize_decision(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if value == "retry":
            value = "retry_with_feedback"
        return {"action": value, "feedback": ""}
    if isinstance(value, dict):
        action = value.get("action", "approve")
        if action == "retry":
            action = "retry_with_feedback"
        return {
            "action": action,
            "feedback": str(value.get("feedback") or ""),
        }
    return {"action": "approve", "feedback": ""}


def make_review_gate(
    phase: str,
    *,
    source_phase: str | None = None,
    label: str | None = None,
) -> Callable[[WorkflowState], dict[str, Any]]:
    source = source_phase or phase

    def review_gate(state: WorkflowState) -> dict[str, Any]:
        config = state.get("config", {})
        result = state.get("phase_results", {}).get(source, {})
        policy = resolve_policy(config, phase)
        threshold = float(
            config.get("review", {}).get("confidence_threshold", 0.75)
        )
        needs_human = policy == "always"
        if policy == "on_exception":
            needs_human = _has_exception(result, threshold)

        payload = {
            "kind": "review_gate",
            "phase": phase,
            "source_phase": source,
            "label": label or phase.replace("_", " ").title(),
            "policy": policy,
            "summary": result.get("summary", "成果物を確認してください"),
            "status": result.get("status", "unknown"),
            "confidence": result.get("confidence"),
            "blocking_issues": result.get("blocking_issues", []),
            "warnings": result.get("warnings", []),
            "artifacts": result.get("artifacts", []),
            "actions": ["approve", "retry_with_feedback", "abort"],
        }
        if needs_human:
            decision = _normalize_decision(interrupt(payload))
            decided_by = "human"
        else:
            decision = {"action": "approve", "feedback": ""}
            decided_by = "policy"

        action = decision["action"]
        if action not in VALID_ACTIONS:
            action = "abort"
            decision["feedback"] = (
                f"Unsupported review action: {decision.get('action')}"
            )

        feedback = dict(state.get("feedback", {}))
        if action == "retry_with_feedback":
            feedback[source] = decision.get("feedback", "")

        reviews = list(state.get("reviews", []))
        reviews.append(
            {
                "t": round(time.time(), 3),
                "phase": phase,
                "source_phase": source,
                "policy": policy,
                "action": action,
                "feedback": decision.get("feedback", ""),
                "decided_by": decided_by,
            }
        )
        events = list(state.get("events", []))
        events.append(
            {
                "t": round(time.time(), 3),
                "type": "review",
                "phase": phase,
                "action": action,
                "decided_by": decided_by,
            }
        )
        route = {
            "approve": "approve",
            "retry_with_feedback": "retry",
            "abort": "abort",
        }[action]
        return {
            "review_route": route,
            "feedback": feedback,
            "reviews": reviews,
            "events": events,
            "aborted": action == "abort",
        }

    review_gate.__name__ = f"review_{phase}"
    return review_gate


def review_router(state: WorkflowState) -> str:
    return state.get("review_route", "abort")
