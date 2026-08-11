"""Compatibility exports for split LLM role modules.

The guarded graph imports roles directly through ``nodes_runtime``. This file
keeps the historical import surface as a stable, logic-free compatibility layer.
"""
from __future__ import annotations

from . import nodes as deterministic
from .roles.assets import (
    _ASSET_DUSK_TERMS,
    _ASSET_NIGHT_TERMS,
    _CLIMAX_ROLES,
    _CUT_ASSET_PATTERN,
    _asset_candidates,
    _asset_time_of_day,
    _canonical_asset_id,
    _code_asset_reason,
    _compact_asset_candidates_for_llm,
    _location_score,
    _ranked_candidates_for_cut,
    _requested_asset_id,
    _requested_asset_map,
    _resolve_requested_asset,
    _score_candidate_for_cut,
    _shortlist_candidates,
    _subject_score,
    _tod_eval,
    asset_curator,
)
from .roles.common import (
    _approved_project_brief,
    _approved_project_value,
    _feedback,
    _llm_error,
    _llm_settings,
    _result_data,
    _review_context,
    _run_role,
    _with_llm_feedback_status,
    _with_llm_metadata,
)
from .roles.director import director
from .roles.downstream import (
    image_video_production,
    post_production,
    provenance,
    review_board,
    visual_qa,
)
from .roles.project import creative_director, executive_producer
from .roles.storyboard import (
    TIME_OF_DAY_VALUES,
    _ASCII_LETTER,
    _FIXED_CUT_DEFAULTS,
    _JAPANESE_CHARACTER,
    _TOD_ALIASES,
    _complete_fixed_storyboard,
    _fallback_japanese_narration,
    _fit_japanese_narration,
    _fixed_storyboard_cuts,
    _normalize_japanese_narration,
    _rescale_cut_durations,
    normalize_time_of_day,
    writer_storyboard,
)
