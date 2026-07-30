"""Canonical runtime node exports for the guarded workflow."""
from __future__ import annotations

from typing import Any

from . import nodes_llm as llm_nodes
from . import pipeline_runtime


executive_producer = llm_nodes.executive_producer
creative_director = llm_nodes.creative_director
writer_storyboard = llm_nodes.writer_storyboard
asset_curator = llm_nodes.asset_curator
director = llm_nodes.director

support_video_creator = pipeline_runtime.support_video_creator
image_video_production = pipeline_runtime.image_video_production
cut_visual_qa = pipeline_runtime.cut_visual_qa
commit_cut_qa = pipeline_runtime.commit_cut_qa
visual_qa = pipeline_runtime.sequence_visual_qa
post_production = pipeline_runtime.post_production
review_board = pipeline_runtime.review_board
provenance = pipeline_runtime.provenance_package


def select_video_shots(
    shots: list[dict[str, Any]],
    *,
    max_video_cuts: int,
    requested_cut_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility helper retained for callers of the former batch runtime."""
    videos = [
        shot
        for shot in shots
        if shot.get("media_requirement") == "video_required"
        or shot.get("media_strategy") == "video"
    ]
    if requested_cut_ids:
        requested = {str(value) for value in requested_cut_ids}
        candidates = [
            shot for shot in videos if str(shot.get("id")) in requested
        ]
    else:
        candidates = videos
    selected = candidates[: max(0, max_video_cuts)]
    selected_ids = {str(shot.get("id")) for shot in selected}
    deferred = [
        shot for shot in videos if str(shot.get("id")) not in selected_ids
    ]
    return selected, deferred
