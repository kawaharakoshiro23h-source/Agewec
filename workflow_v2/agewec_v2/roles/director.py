"""Director role and targeted direction-plan revision."""
from __future__ import annotations

from typing import Any

from ..fallbacks import director as deterministic
from ..state import WorkflowState

from .common import (
    _approved_project_brief, _result_data, _review_context, _run_role,
)
from .assets import _canonical_asset_id

def director(state: WorkflowState) -> dict[str, Any]:
    storyboard = _result_data(state, "writer_storyboard")
    assets = _result_data(state, "asset_curator")
    concept = _result_data(state, "creative_director")
    if not storyboard or not assets or not concept:
        return deterministic._complete(
            state,
            "director",
            summary="上流成果物不足のため演出設計不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Concept、Storyboard、AssetSelectionが必要"],
        )
    context = _review_context(state, "director")
    target_cut_id = context.get("target_cut_id")
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)
    config = state.get("config", {})
    production = config.get("production", {})
    backend = str(production.get("backend") or "mock").lower()
    default_model = str(production.get("model") or "") or None
    runway_models = dict(config.get("runway", {}).get("models", {}))
    available_models = [
        {
            "model": name,
            "generation_modes": list(
                spec.get(
                    "generation_modes",
                    ["image_to_video", "text_to_video"],
                )
            ),
            "allowed_seconds": list(spec.get("allowed_seconds", [])),
            "resolutions": list(spec.get("resolutions", [])),
            "has_native_audio": bool(spec.get("has_native_audio", False)),
            "cost_per_second_usd": float(
                spec.get("cost_per_second_usd", 0.0)
            ),
        }
        for name, spec in runway_models.items()
    ] if backend == "runway" else []

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        cut_map = {int(cut["id"]): cut for cut in storyboard["cuts"]}
        if target_cut_id is not None and target_cut_id not in cut_map:
            raise ValueError(f"Unknown target_cut_id: {target_cut_id}")
        assignment_map = {
            int(item["cut_id"]): item
            for item in assets.get("asset_assignments", [])
        }
        if set(assignment_map) != set(cut_map):
            missing = sorted(set(cut_map) - set(assignment_map))
            raise ValueError(
                "Asset assignment is required for every cut; missing: "
                + ", ".join(map(str, missing))
            )
        new_shots: dict[int, dict[str, Any]] = {}
        invalid = []
        for direction in data["shots"]:
            cut_id = int(direction["cut_id"])
            mode = str(
                direction.get("generation_mode") or "image_to_video"
            )
            selected_model = (
                str(direction.get("model") or default_model or "") or None
            )
            if cut_id not in cut_map:
                invalid.append(f"unknown cut={cut_id}")
                continue
            if target_cut_id is not None and cut_id != target_cut_id:
                continue
            if backend == "runway" and selected_model not in runway_models:
                invalid.append(
                    f"cut={cut_id}, model={selected_model or '(empty)'} "
                    "is not configured for Runway"
                )
                continue
            # text_to_video は参照写真を使わないため、素材の割当検証を行わない。
            # 画像が無いことを理由に自動でこのモードへ落とすことはしない
            # （Schema側で asset_id との整合を検証済み）。
            if mode == "text_to_video":
                asset = None
            else:
                raw_asset_id = direction["asset_id"]
                asset_id = _canonical_asset_id(raw_asset_id)
                assignment = assignment_map[cut_id]
                choices = [assignment["primary"], *assignment["alternatives"]]
                asset_map = {item["asset_id"]: item for item in choices}
                if asset_id not in asset_map:
                    invalid.append(
                        f"cut={cut_id}, asset={raw_asset_id} "
                        f"(normalized={asset_id}) is not assigned to cut"
                    )
                    continue
                asset = asset_map[asset_id]
            if (
                direction.get("deviation_reason")
                and not direction["deviation_reason"].strip()
            ):
                direction["deviation_reason"] = None
            new_shots[cut_id] = {
                **cut_map[cut_id],
                "generation_mode": mode,
                "model": selected_model if backend == "runway" else None,
                "asset": asset,
                "positive_prompt": direction["positive_prompt"],
                "negative_prompt": direction["negative_prompt"],
                "camera_motion": direction["camera_motion"],
                "motion_intensity": direction["motion_intensity"],
                "rationale": direction["rationale"],
                "camera_intent_alignment": direction[
                    "camera_intent_alignment"
                ],
                "deviation_reason": direction.get("deviation_reason"),
            }
        if invalid:
            raise ValueError(
                "Director returned invalid IDs/assets: " + ", ".join(invalid)
            )
        existing = (
            _result_data(state, "director").get("shots", [])
            if target_cut_id is not None
            else []
        )
        merged = {
            int(shot["id"]): shot
            for shot in existing
            if int(shot["id"]) in cut_map
        }
        merged.update(new_shots)
        if target_cut_id is not None and target_cut_id not in new_shots:
            raise ValueError(
                f"Targeted retry must return cut {target_cut_id}"
            )
        if set(merged) != set(cut_map):
            missing = sorted(set(cut_map) - set(merged))
            raise ValueError(
                "Director must return exactly one shot per cut; missing: "
                + ", ".join(map(str, missing))
            )
        shots = [merged[cut_id] for cut_id in sorted(merged)]
        return {
            "shots": shots,
            "continuity_checks": data["continuity_checks"],
            "targeted_revision_cut_id": target_cut_id,
            "technical_parameters_status": "pending_support_video_creator",
        }

    return _run_role(
        state,
        phase="director",
        upstream={
            "project_brief": _approved_project_brief(state),
            "creative_concept": concept,
            "storyboard": storyboard,
            "asset_manifest": assets,
            "camera_intent": concept.get("camera_intent", {}),
            "target_cut_id": target_cut_id,
            "existing_direction_plan": (
                _result_data(state, "director")
                if target_cut_id is not None
                else {}
            ),
            "locked_cut_rule": (
                "When target_cut_id is supplied, return only that cut. "
                "All other approved shots are locked."
            ),
            "video_model_policy": {
                "backend": backend,
                "default_model": default_model,
                "available_models": available_models,
            },
        },
        summary=lambda data: (
            f"LLMが{len(data['shots'])}カットの演出指示を確定"
        ),
        fallback=deterministic.director,
        transform=transform,
    )
