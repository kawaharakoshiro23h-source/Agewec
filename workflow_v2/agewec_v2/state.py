"""Shared state and phase names for the isolated v2 workflow."""
from __future__ import annotations

from typing import Any, TypedDict


PHASES = (
    "executive_producer",
    "creative_director",
    "writer_storyboard",
    "asset_curator",
    "director",
    "support_video_creator",
    "image_video_production",
    "cut_visual_qa",
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
    review_context: dict[str, dict[str, Any]]
    current_phase: str
    review_route: str
    review_target_phase: str
    reviews: list[dict[str, Any]]
    events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    production_requests: dict[str, dict[str, Any]]
    production_queue: list[int]
    current_cut_id: int | None
    generated_cut_ids: list[int]
    approved_cut_ids: list[int]
    failed_cut_ids: list[int]
    cut_attempts: dict[str, int]
    cut_results: dict[str, dict[str, Any]]
    production_artifacts: dict[str, dict[str, Any]]
    cut_qa_results: dict[str, dict[str, Any]]
    cut_qa_route: str
    final_output: str | None
    provenance_output: str | None
    process_report_output: str | None
    submission_manifest: str | None
    aborted: bool
