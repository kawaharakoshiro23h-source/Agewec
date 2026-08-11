"""Compatibility exports for deterministic fallbacks.

New code imports ``agewec_v2.fallbacks`` modules directly. Existing symbols
remain available here as a stable, logic-free compatibility surface.
"""
from __future__ import annotations

from .paths import PROJECT_ROOT, WORKFLOW_ROOT
from .fallbacks.assets import _load_catalog, _local_asset_path, asset_curator
from .fallbacks.common import (
    AWARD_GENRES,
    _SECRET_KEY_HINTS,
    _SECRET_TOKEN_KEYS,
    _complete,
    _cut_path,
    _is_secret_key,
    _phase_feedback,
    _sanitized,
    _work_path,
)
from .fallbacks.director import director
from .fallbacks.legacy_media import (
    _comfy_production,
    _mock_production,
    image_video_production,
    post_production,
    provenance,
    review_board,
    visual_qa,
)
from .fallbacks.planning import (
    _limit_storyboard_cuts,
    creative_director,
    executive_producer,
    writer_storyboard,
)
