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
        return {
            "action": value,
            "feedback": "",
            "target_cut_id": None,
            "correction_type": "",
            "project_updates": {},
            "cut_route": "",
        }
    if isinstance(value, dict):
        action = value.get("action", "approve")
        if action == "retry":
            action = "retry_with_feedback"
        target_cut_id = value.get("target_cut_id")
        if target_cut_id is not None:
            target_cut_id = int(target_cut_id)
            if target_cut_id <= 0:
                raise ValueError("target_cut_id must be a positive integer")
        project_updates = value.get("project_updates") or {}
        if not isinstance(project_updates, dict):
            raise ValueError("project_updates must be an object")
        return {
            "action": action,
            "feedback": str(value.get("feedback") or ""),
            "target_cut_id": target_cut_id,
            "correction_type": str(value.get("correction_type") or ""),
            "project_updates": project_updates,
            "cut_route": str(value.get("cut_route") or ""),
        }
    return {
        "action": "approve",
        "feedback": "",
        "target_cut_id": None,
        "correction_type": "",
        "project_updates": {},
        "cut_route": "",
    }


def _review_target_phase(
    phase: str,
    source: str,
    correction_type: str,
) -> str:
    if phase not in {"director", "support_video_creator"}:
        return source
    mapping = {
        "asset": "asset_curator",
        "asset_selection": "asset_curator",
        "storyboard": "writer_storyboard",
        "structure": "writer_storyboard",
        "concept": "creative_director",
        "direction": "director",
        "prompt": "director",
        "camera": "director",
        "generation_parameters": "support_video_creator",
        "production_parameters": "support_video_creator",
    }
    return mapping.get(correction_type, source)


def _apply_project_updates(
    project: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    if not updates:
        return project
    allowed = {
        "target_award",
        "theme",
        "target_duration_seconds",
        "audience",
        "deliverable",
    }
    unknown = sorted(set(updates) - allowed)
    if unknown:
        raise ValueError(
            "Unsupported project_updates: " + ", ".join(unknown)
        )
    merged = dict(project)
    merged.update(updates)
    if "target_duration_seconds" in merged:
        duration = float(merged["target_duration_seconds"])
        if duration <= 0:
            raise ValueError("target_duration_seconds must be positive")
        merged["target_duration_seconds"] = duration
    return merged


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
        force_human = bool(
            phase == "final_submission"
            and config.get("final_submission", {}).get(
                "require_human",
                True,
            )
        )
        needs_human = policy == "always" or force_human
        if policy == "on_exception":
            needs_human = (
                _has_exception(result, threshold) or force_human
            )

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
            "require_human": force_human,
            "retry_fields": {
                "feedback": "修正内容",
                "target_cut_id": "対象カットだけを修正する場合のID",
                "correction_type": (
                    "direction / asset / storyboard / concept"
                ),
                "project_updates": (
                    "Executive Producerで構造化条件を変更する場合"
                ),
            },
        }
        if phase == "cut_visual_qa":
            current = state.get("current_cut_id")
            payload.update(
                {
                    "cut_id": current,
                    "review_page": state.get("cut_review_page"),
                    "cut_routes": {
                        "director": "演出・プロンプトを修正して再生成",
                        "asset_curator": "素材を変更して再生成",
                        "support_video_creator": "生成設定を変更して再生成",
                        "image_video_production": "同じ条件で再生成(seed変更)",
                    },
                }
            )
        if phase == "final_submission":
            post = (
                state.get("phase_results", {})
                .get("post_production", {})
                .get("data", {})
            )
            payload.update(
                {
                    "final_video": state.get("final_output"),
                    "final_technical_qa": post.get(
                        "technical_qa",
                        {},
                    ),
                    "review_board": result.get("data", {}),
                    "unresolved_warnings": [
                        warning
                        for phase_result in state.get(
                            "phase_results",
                            {},
                        ).values()
                        for warning in phase_result.get("warnings", [])
                    ],
                }
            )
        if needs_human:
            decision = _normalize_decision(interrupt(payload))
            decided_by = "human"
        else:
            decision = _normalize_decision("approve")
            decided_by = "policy"

        action = decision["action"]
        if action not in VALID_ACTIONS:
            action = "abort"
            decision["feedback"] = (
                f"Unsupported review action: {decision.get('action')}"
            )

        target_phase = _review_target_phase(
            phase,
            source,
            decision.get("correction_type", ""),
        )
        feedback = dict(state.get("feedback", {}))
        review_context = dict(state.get("review_context", {}))
        project = dict(state.get("project", {}))
        if action == "retry_with_feedback":
            feedback[target_phase] = decision.get("feedback", "")
            review_context[target_phase] = {
                "source_review": phase,
                "target_cut_id": decision.get("target_cut_id"),
                "correction_type": decision.get("correction_type", ""),
            }
            if source == "executive_producer":
                project = _apply_project_updates(
                    project,
                    decision.get("project_updates", {}),
                )
        elif action == "approve":
            feedback.pop(source, None)
            review_context.pop(source, None)

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
                "target_phase": target_phase,
                "target_cut_id": decision.get("target_cut_id"),
                "correction_type": decision.get("correction_type", ""),
                "project_updates": decision.get("project_updates", {}),
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
        update = {
            "review_route": route,
            "review_target_phase": target_phase,
            "feedback": feedback,
            "review_context": review_context,
            "project": project,
            "reviews": reviews,
            "events": events,
            "aborted": action == "abort",
        }
        # Cut QAレビューで人間が差し戻し先を選んだ場合、その判断を
        # cut_id 別に保存する。commit_cut_qa がAI判定より優先して読む。
        if phase == "cut_visual_qa" and decision.get("cut_route"):
            current = state.get("current_cut_id")
            if current is not None:
                decisions = dict(state.get("human_cut_qa_decisions", {}))
                decisions[str(int(current))] = {
                    "verdict": "revise",
                    "route": decision["cut_route"],
                    "feedback": decision.get("feedback", ""),
                    "issue_class": decision.get(
                        "correction_type", "human_review"
                    ),
                }
                update["human_cut_qa_decisions"] = decisions
        return update

    review_gate.__name__ = f"review_{phase}"
    return review_gate


def review_router(state: WorkflowState) -> str:
    return state.get("review_route", "abort")
