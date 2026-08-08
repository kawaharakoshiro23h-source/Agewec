"""Shared runtime helpers with no phase routing policy."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from ..fallbacks import common as deterministic
from ..state import WorkflowState


def _result_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _approved_project_value(
    state: WorkflowState,
    key: str,
    default: Any,
) -> Any:
    """承認済みProjectBriefの値を優先する。"""
    brief = _result_data(state, "executive_producer")
    if key in brief:
        return brief[key]
    return state.get("project", {}).get(key, default)


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _attempt_json_path(
    state: WorkflowState,
    cut_id: int,
    attempt: int,
    kind: str,
) -> Path:
    """カット内のattempt別メタデータJSONの保存先を返す。"""
    return deterministic._cut_path(
        state,
        cut_id,
        f"attempt_{int(attempt):02d}_{kind}.json",
    )


def _stable_seed(run_id: str, cut_id: int, attempt: int = 0) -> int:
    """run_id・cut_id・試行回数から決定論的なseedを作る。

    attempt を含めることで、同じ条件での「再生成」が別の結果になる。
    attempt が同じなら常に同じseed＝再現性は保たれる。
    """
    digest = hashlib.sha256(f"{run_id}:{cut_id}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


def _ltx_frame_count(
    seconds: float,
    fps: int,
    *,
    multiple: int,
    offset: int,
) -> int:
    raw = max(1, round(seconds * fps))
    # Never round down: Phase 08 may trim a long clip, but it must not invent
    # missing frames when a generated clip is shorter than the storyboard.
    steps = max(0, math.ceil((raw - offset) / multiple))
    return max(offset, offset + steps * multiple)


def _ratio_dimensions(ratio: str) -> tuple[int, int]:
    """Convert a Runway ratio such as ``1280:720`` to dimensions."""
    try:
        width_text, height_text = ratio.split(":", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"不正なRunway ratio: {ratio!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"不正なRunway ratio: {ratio!r}")
    return width, height
