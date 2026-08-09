"""Cross-phase contracts that must survive module and directory refactors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import PHASES


@dataclass(frozen=True)
class PhaseContract:
    phase: str
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    side_effects: tuple[str, ...] = ()


PHASE_CONTRACTS: dict[str, PhaseContract] = {
    "executive_producer": PhaseContract(
        "executive_producer", ("project",), ("project_brief",)
    ),
    "creative_director": PhaseContract(
        "creative_director", ("project_brief",), ("creative_concept",)
    ),
    "writer_storyboard": PhaseContract(
        "writer_storyboard", ("creative_concept",), ("storyboard",)
    ),
    "asset_curator": PhaseContract(
        "asset_curator", ("storyboard", "asset_catalog"), ("asset_manifest",)
    ),
    "director": PhaseContract(
        "director", ("storyboard", "asset_manifest"), ("direction_plan",)
    ),
    "support_video_creator": PhaseContract(
        "support_video_creator",
        ("direction_plan", "backend_capabilities"),
        ("production_requests", "production_queue"),
    ),
    "image_video_production": PhaseContract(
        "image_video_production",
        ("production_requests", "production_queue"),
        ("production_artifacts", "cut_results"),
        ("video_generation", "cost_ledger"),
    ),
    "cut_visual_qa": PhaseContract(
        "cut_visual_qa",
        ("production_artifacts",),
        ("cut_qa_results",),
        ("representative_frames",),
    ),
    "visual_qa": PhaseContract(
        "visual_qa",
        ("approved_cut_ids", "production_artifacts"),
        ("sequence_qa",),
    ),
    "post_production": PhaseContract(
        "post_production",
        ("approved_cut_ids", "production_artifacts"),
        ("final_output",),
        ("normalized_clips", "final_video"),
    ),
    "review_board": PhaseContract(
        "review_board", ("final_output", "sequence_qa"), ("review_verdict",)
    ),
    "final_submission": PhaseContract(
        "final_submission", ("review_verdict", "final_output"), ("human_decision",)
    ),
    "provenance": PhaseContract(
        "provenance",
        ("final_output", "phase_results", "events"),
        ("submission_package", "process_report"),
        ("submission_files",),
    ),
}


def missing_contract_phases() -> set[str]:
    return set(PHASES) - set(PHASE_CONTRACTS)


def effective_target_cut_id(
    requested_cut_id: int | None,
    shots: list[dict[str, Any]],
    existing_requests: dict[str, dict[str, Any]],
) -> int | None:
    """Allow a targeted rebuild only when a complete request base exists.

    A target can originate in Director review before Phase 05.5 has ever built
    all requests.  In that case narrowing to one cut would silently omit every
    other cut, so the first/partial build must remain a full build.
    """
    if requested_cut_id is None:
        return None
    expected = {int(shot["id"]) for shot in shots}
    covered = {int(cut_id) for cut_id in existing_requests}
    return requested_cut_id if covered.issuperset(expected) else None


def preserves_existing_artifact(route: str) -> bool:
    """Only a direct same-cut regeneration may retain the previous artifact."""
    return route == "image_video_production"

