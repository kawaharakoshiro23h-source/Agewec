"""Shared deterministic state, path, and sanitization helpers."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..paths import runtime_paths
from ..state import WorkflowState

AWARD_GENRES = {
    "夜景賞": "イルミネーション・夜景",
    "観光賞": "観光スポット",
    "環境賞": "公園",
    "DX賞": None,
}


def _work_path(state: WorkflowState, *parts: str) -> Path:
    """実行単位（run_id）で分離された作業ディレクトリのパスを返す。

        work/runs/<run_id>/<parts...>

    実行ごとにフォルダを分けることで、過去runとの上書き・混同を防ぐ。
    複数モデルを比較する際も、成果物が互いに潰し合わない。
    run_id が無い場合（単体テスト等）は従来どおり work/ 直下を使う。
    """
    run_id = str(state.get("run_id") or "")
    return runtime_paths(state.get("config", {})).work_path(
        run_id or None,
        *parts,
    )


def _cut_path(state: WorkflowState, cut_id: int, *parts: str) -> Path:
    """カット単位のディレクトリ（work/runs/<run_id>/cuts/cut_XX/...）。"""
    return _work_path(state, "cuts", f"cut_{int(cut_id):02d}", *parts)


def _phase_feedback(state: WorkflowState, phase: str) -> str:
    return state.get("feedback", {}).get(phase, "")


def _complete(
    state: WorkflowState,
    phase: str,
    *,
    summary: str,
    data: dict[str, Any],
    artifacts: list[dict[str, Any]] | None = None,
    status: str = "success",
    confidence: float | None = None,
    blocking_issues: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    attempts = dict(state.get("attempts", {}))
    attempts[phase] = attempts.get(phase, 0) + 1
    phase_results = dict(state.get("phase_results", {}))
    previous_result = phase_results.get(phase, {})
    feedback_received = _phase_feedback(state, phase)
    feedback_context = state.get("review_context", {}).get(phase, {})
    feedback_origin = (
        feedback_context.get("feedback_origin")
        if feedback_received
        else None
    )
    result = {
        "phase": phase,
        "status": status,
        "summary": summary,
        "data": data,
        "artifacts": artifacts or [],
        "confidence": confidence,
        "blocking_issues": blocking_issues or [],
        "warnings": warnings or [],
        "attempt": attempts[phase],
        # Deterministic nodes do not interpret free-form feedback. Keep receipt
        # and application separate so provenance never claims that merely
        # carrying a string changed the artifact.
        "feedback_received": feedback_received,
        "feedback_origin": feedback_origin,
        "feedback_applied": False,
        "feedback_status": (
            "received_not_applied_by_deterministic_node"
            if feedback_received
            else "not_provided"
        ),
    }
    if previous_result.get("data") is not None:
        result["previous_data"] = previous_result.get("data")
    phase_results[phase] = result
    all_artifacts = list(state.get("artifacts", []))
    all_artifacts.extend(artifacts or [])
    events = list(state.get("events", []))
    events.append(
        {
            "t": round(time.time(), 3),
            "type": "phase_completed",
            "phase": phase,
            "status": status,
            "attempt": attempts[phase],
            "summary": summary,
        }
    )
    return {
        "current_phase": phase,
        "phase_results": phase_results,
        "attempts": attempts,
        "events": events,
        "artifacts": all_artifacts,
    }



_SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "authorization",
)
# 「token」を部分一致で弾くと prompt_tokens / completion_tokens / total_tokens
# といった利用統計まで潰れてしまう（レポートでトークン数を出せなくなる）。
# 認証情報として使われる語だけを完全一致で秘匿する。
_SECRET_TOKEN_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "id_token",
        "bearer_token",
        "session_token",
    }
)


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower()
    if any(hint in lowered for hint in _SECRET_KEY_HINTS):
        return True
    return lowered in _SECRET_TOKEN_KEYS


def _sanitized(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, child in value.items():
            if _is_secret_key(key):
                clean[key] = "***"
            else:
                clean[key] = _sanitized(child)
        return clean
    if isinstance(value, list):
        return [_sanitized(item) for item in value]
    return value
