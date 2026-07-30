"""Shared state and phase names for the isolated v2 workflow."""
from __future__ import annotations

from typing import Any, TypedDict


PHASES = (
    "executive_producer",
    "creative_director",
    "writer_storyboard",
    "asset_curator",
    "director",
    "image_video_production",
    "visual_qa",
    "post_production",
    "review_board",
    "final_submission",
    "provenance",
)


class WorkflowState(TypedDict, total=False):
    run_id: str
    project: dict[str, Any]
    config: dict[str, Any]
    phase_results: dict[str, dict[str, Any]]
    attempts: dict[str, int]
    feedback: dict[str, str]
    current_phase: str
    review_route: str
    reviews: list[dict[str, Any]]
    events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    final_output: str | None
    aborted: bool
