"""Shared role execution, feedback, and LLM metadata helpers."""
from __future__ import annotations

import copy
from typing import Any, Callable

from ..fallbacks import common as deterministic
from ..llm import LLMSettings, RoleRunner
from ..state import WorkflowState

def _result_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _approved_project_brief(state: WorkflowState) -> dict[str, Any]:
    """下流LLMへ渡す、承認済みの企画契約を返す。

    `source_project`は証跡用の初期入力であり、承認後の指示ではない。
    保存済みProjectBrief自体は変更せず、LLM入力用のコピーから
    のみ除外する。
    """
    brief = copy.deepcopy(_result_data(state, "executive_producer"))
    brief.pop("source_project", None)
    return brief


def _approved_project_value(
    state: WorkflowState,
    key: str,
    default: Any,
) -> Any:
    """ProjectBriefを優先し、未生成時だけ初期projectへ戻る。"""
    brief = _result_data(state, "executive_producer")
    if key in brief:
        return brief[key]
    return state.get("project", {}).get(key, default)


def _feedback(state: WorkflowState, phase: str) -> str:
    return state.get("feedback", {}).get(phase, "")


def _review_context(
    state: WorkflowState,
    phase: str,
) -> dict[str, Any]:
    return dict(state.get("review_context", {}).get(phase, {}))


def _llm_settings(state: WorkflowState) -> LLMSettings:
    return LLMSettings.from_sources(state.get("config", {}))


def _with_llm_metadata(
    update: dict[str, Any],
    phase: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    phase_results = dict(update["phase_results"])
    result = dict(phase_results[phase])
    result["llm"] = metadata
    phase_results[phase] = result
    update["phase_results"] = phase_results
    return update


def _with_llm_feedback_status(
    update: dict[str, Any],
    phase: str,
    feedback: str,
) -> dict[str, Any]:
    """Record delivery and observable output change without overclaiming.

    A changed JSON artifact is evidence that a retry produced a new result, but
    it is not proof that every semantic instruction was followed. Human review
    remains the authority for that judgment.
    """
    phase_results = dict(update["phase_results"])
    result = dict(phase_results[phase])
    result["feedback_received"] = feedback
    if feedback:
        previous = result.get("previous_data")
        current = result.get("data")
        result["feedback_applied"] = None
        result["feedback_status"] = "delivered_to_llm_pending_human_verification"
        result["feedback_application_evidence"] = (
            "output_changed"
            if previous is not None and previous != current
            else "output_unchanged_or_no_baseline"
        )
    else:
        result["feedback_applied"] = False
        result["feedback_status"] = "not_provided"
    phase_results[phase] = result
    update["phase_results"] = phase_results
    return update


def _llm_error(
    state: WorkflowState,
    phase: str,
    exc: Exception,
) -> dict[str, Any]:
    return deterministic._complete(
        state,
        phase,
        summary=f"LLM実行失敗: {type(exc).__name__}",
        data={},
        status="error",
        confidence=0.0,
        blocking_issues=[f"{type(exc).__name__}: {exc}"],
    )


def _run_role(
    state: WorkflowState,
    *,
    phase: str,
    upstream: dict[str, Any],
    summary: Callable[[dict[str, Any]], str],
    fallback: Callable[[WorkflowState], dict[str, Any]],
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    try:
        settings = _llm_settings(state)
    except Exception as exc:
        return _llm_error(state, phase, exc)
    if not settings.enabled:
        return fallback(state)

    try:
        feedback = _feedback(state, phase)
        run = RoleRunner(state.get("config", {})).run(
            role=phase,
            upstream=upstream,
            feedback=feedback,
        )
        raw = run.output.model_dump(mode="json")
        data = transform(raw) if transform else raw
        update = deterministic._complete(
            state,
            phase,
            summary=summary(data),
            data=data,
            artifacts=artifacts,
            confidence=float(data.get("confidence", 0.9)),
            warnings=warnings,
        )
        update = _with_llm_feedback_status(update, phase, feedback)
        return _with_llm_metadata(update, phase, run.metadata)
    except Exception as exc:
        if settings.strict_mode:
            return _llm_error(state, phase, exc)
        update = fallback(state)
        result = dict(update["phase_results"][phase])
        result["warnings"] = list(result.get("warnings", [])) + [
            f"LLM失敗のため決定的フォールバックを使用: {type(exc).__name__}: {exc}"
        ]
        result["llm"] = {
            "provider": settings.provider,
            "model": settings.model,
            "fallback": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        phase_results = dict(update["phase_results"])
        phase_results[phase] = result
        update["phase_results"] = phase_results
        return update
