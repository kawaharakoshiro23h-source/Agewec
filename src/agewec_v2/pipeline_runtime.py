"""Compatibility exports for the split runtime phases.

New production code imports ``agewec_v2.phases`` modules directly.  This
module retains the historical symbols for tests and third-party callers during
the migration window.
"""
from __future__ import annotations

from . import review_page
from .media_tools import (
    decode_check,
    downscale_image,
    extract_representative_frames,
    probe_media,
)
from .phases.common import (
    _approved_project_value,
    _attempt_json_path,
    _json_write,
    _ltx_frame_count,
    _ratio_dimensions,
    _result_data,
    _stable_seed,
)
from .phases.cut_qa import commit_cut_qa, cut_visual_qa
from .phases.post_production import post_production, review_board
from .phases.production import (
    _generate_comfy,
    _prepare_runway_input_image,
    _run_cost_estimate,
    _technical_request_signature,
    _unchanged_technical_retry,
    _video_backend,
    _video_budget,
    image_video_production,
)
from .phases.provenance import (
    _copy_cut_sources,
    _sha256_of,
    provenance_package,
)
from .phases.reporting import (
    _BACKEND_PRESENTATION,
    _CARD_COVERED_ITEMS,
    _HOME_PATTERN,
    _PHASE_PRESENTATION,
    _compact_text,
    _cut_media_paths,
    _decision_log,
    _feedback_actual_items,
    _format_markdown_value,
    _generation_conditions,
    _guide_for_backend,
    _human_intervention_summary,
    _llm_usage_totals,
    _phase_actual_items,
    _phase_visual_cards,
    _portable_path,
    _process_html,
    _process_markdown,
    _render_html_value,
    _request_summary,
    _run_summary_html,
    _sha256,
    _video_cost_summary,
)
from .phases.sequence_qa import sequence_visual_qa
from .phases.support_video import (
    _GENERATION_MODES,
    _runway_request_parameters,
    support_video_creator,
)
