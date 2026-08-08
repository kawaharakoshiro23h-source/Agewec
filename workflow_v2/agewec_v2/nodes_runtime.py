"""Canonical runtime node exports for the guarded workflow.

【本番経路: 正】graph_safe が読み込むノードの入口。

    呼ばれる側: graph_safe.py
    使う側    : nodes_llm（役割別LLM）／phases（実処理）

企画系フェーズは `nodes_llm` を、生成・QA・編集・証跡は `phases`配下を束ねる。
新しいノードは責務に対応するPhaseモジュールに置き、ここから公開する。
"""
from __future__ import annotations

from typing import Any

from . import nodes_llm as llm_nodes
from .phases import (
    cut_qa,
    post_production as post,
    production,
    provenance as provenance_phase,
    sequence_qa,
    support_video,
)


executive_producer = llm_nodes.executive_producer
creative_director = llm_nodes.creative_director
writer_storyboard = llm_nodes.writer_storyboard
asset_curator = llm_nodes.asset_curator
director = llm_nodes.director

support_video_creator = support_video.support_video_creator
image_video_production = production.image_video_production
cut_visual_qa = cut_qa.cut_visual_qa
commit_cut_qa = cut_qa.commit_cut_qa
visual_qa = sequence_qa.sequence_visual_qa
post_production = post.post_production
review_board = post.review_board
provenance = provenance_phase.provenance_package


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
