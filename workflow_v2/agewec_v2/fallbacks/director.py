"""Deterministic direction-plan fallback."""
from __future__ import annotations

from typing import Any

from ..state import WorkflowState

from .common import _complete

def director(state: WorkflowState) -> dict[str, Any]:
    phase = "director"
    cuts = state["phase_results"]["writer_storyboard"]["data"]["cuts"]
    assignments = {
        int(item["cut_id"]): item
        for item in state["phase_results"]["asset_curator"]["data"].get(
            "asset_assignments",
            [],
        )
    }
    camera_intent = (
        state["phase_results"]["creative_director"]["data"]
        .get("camera_intent", {})
    )
    context = state.get("review_context", {}).get(phase, {})
    target_cut_id = context.get("target_cut_id")
    production = state.get("config", {}).get("production", {})
    default_model = str(production.get("model") or "") or None
    existing = (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
        .get("shots", [])
    )
    shot_map = {
        int(shot["id"]): shot
        for shot in existing
        if target_cut_id is not None
    }
    for cut in cuts:
        cut_id = int(cut["id"])
        if target_cut_id is not None and cut_id != int(target_cut_id):
            continue
        assignment = assignments.get(cut_id, {})
        asset = assignment.get("primary", {})
        motion = "slow stable push-in with subtle parallax"
        shot_map[cut_id] = (
            {
                **cut,
                "model": default_model,
                "asset": asset,
                "positive_prompt": (
                    f"{cut['scene']}. {motion}. Deep blue and warm amber color "
                    "palette, realistic documentary cinematography."
                ),
                "negative_prompt": "",
                "camera_motion": motion,
                "motion_intensity": "subtle",
                "rationale": (
                    "実在景観を維持しながら静止画へ奥行きを加えるため"
                ),
                "camera_intent_alignment": (
                    camera_intent.get(
                        "viewer_experience",
                        "全体の安定した映像方針",
                    )
                ),
                "deviation_reason": None,
            }
        )
    shot_plan = [shot_map[key] for key in sorted(shot_map)]
    missing = sorted(
        {int(cut["id"]) for cut in cuts} - set(shot_map)
    )
    plan = {
        "shots": shot_plan,
        "continuity_checks": [
            "deep blueとwarm amberを維持",
            "建築・地形を過度に変形させない",
            "カメラ移動は緩やかにする",
        ],
        "targeted_revision_cut_id": target_cut_id,
        "technical_parameters_status": "pending_support_video_creator",
    }
    return _complete(
        state,
        phase,
        summary=f"{len(shot_plan)}カットの素材・演出指示を確定",
        data=plan,
        confidence=0.85,
        blocking_issues=(
            [] if not missing else [f"演出未作成カット: {missing}"]
        ),
    )
