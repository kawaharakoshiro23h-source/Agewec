"""Deterministic asset-catalog and selection fallback."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..paths import runtime_paths
from ..state import WorkflowState

from .common import AWARD_GENRES, _complete

def _load_catalog(config: dict[str, Any] | None = None) -> dict[str, Any]:
    path = runtime_paths(config).asset_catalog
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _local_asset_path(
    photo: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str | None:
    layout = runtime_paths(config)
    # 1) カタログに local_path があればそれを優先（新命名 asset-XXX_... に対応）
    rel = photo.get("local_path")
    if rel:
        candidate = layout.resolve_project(rel)
        if candidate.exists():
            return str(candidate)
    # 2) 後方互換: image_url のファイル名から推測（旧カタログ用）
    image_url = photo.get("image_url", "")
    name = unquote(Path(urlparse(image_url).path).name)
    candidate = layout.assets_root / name
    return str(candidate) if name and candidate.exists() else None


def asset_curator(state: WorkflowState) -> dict[str, Any]:
    phase = "asset_curator"
    cuts = (
        state.get("phase_results", {})
        .get("writer_storyboard", {})
        .get("data", {})
        .get("cuts", [])
    )
    config = state.get("config", {})
    catalog = _load_catalog(config)
    candidates = []
    for index, photo in enumerate(catalog.get("photos", []), start=1):
        local_path = _local_asset_path(photo, config)
        candidates.append(
            {
                "asset_id": f"asset-{index:03d}",
                "title": photo.get("title", ""),
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": photo.get("genres", []),
                "areas": photo.get("areas", []),
                "local_path": local_path,
                "local_available": bool(local_path),
                "usage_scope": "agewec_submission",
                "rights_status": "approved_for_agewec_submission",
            }
        )
    candidates.sort(
        key=lambda item: (
            not item["local_available"],
            item["asset_id"],
        )
    )
    context = state.get("review_context", {}).get(phase, {})
    target_cut_id = context.get("target_cut_id")
    existing = (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
        .get("asset_assignments", [])
    )
    assignments = {
        int(item["cut_id"]): item
        for item in existing
        if target_cut_id is not None
    }
    for index, cut in enumerate(cuts):
        cut_id = int(cut["id"])
        if target_cut_id is not None and cut_id != int(target_cut_id):
            continue
        if not candidates:
            break
        primary = candidates[index % len(candidates)]
        alternative = candidates[(index + 1) % len(candidates)]
        assignments[cut_id] = {
            "cut_id": cut_id,
            "primary": {
                **primary,
                "selection_reason": (
                    f"{cut['location']}の{cut['visual_role']}に利用する"
                ),
            },
            "alternatives": [
                {
                    **alternative,
                    "selection_reason": "構図または時刻帯の代替候補",
                }
            ]
            if alternative["asset_id"] != primary["asset_id"]
            else [],
        }
    missing_cut_ids = sorted(
        {int(cut["id"]) for cut in cuts} - set(assignments)
    )
    assignment_list = [assignments[key] for key in sorted(assignments)]
    selected = [
        {**item["primary"], "cut_id": item["cut_id"]}
        for item in assignment_list
    ]
    manifest = {
        "catalog_source": catalog.get("source"),
        "available_candidate_count": len(candidates),
        "asset_assignments": assignment_list,
        "selected_assets": selected,
        "unassigned_cut_ids": missing_cut_ids,
        "rights_check_required": False,
        "usage_scope": "agewec_submission",
        "targeted_revision_cut_id": target_cut_id,
    }
    return _complete(
        state,
        phase,
        summary=f"{len(assignment_list)}カットへ公式素材を割当",
        data=manifest,
        confidence=0.88 if not missing_cut_ids else 0.4,
        blocking_issues=(
            []
            if not missing_cut_ids
            else [f"素材未割当カット: {missing_cut_ids}"]
        ),
        warnings=(
            ["一部の選定素材はローカル未取得"]
            if any(not item.get("local_available") for item in selected)
            else []
        ),
    )
