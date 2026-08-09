"""Phase 05.5 backend-native production request construction."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fallbacks import common as deterministic
from ..backends import (
    Capabilities,
    UnsupportedDurationError,
)
from ..phase_contracts import effective_target_cut_id
from ..state import WorkflowState

from .common import (
    _ltx_frame_count, _ratio_dimensions, _result_data, _stable_seed,
)
from .production import _run_cost_estimate


# カット単位で選べる生成方式。Director が決め、Phase 05.5 で検証する。
#   image_to_video … 公式写真を動かす（既定。provenance が最も強い）
#   text_to_video  … 参照写真なしで文章から生成する
_GENERATION_MODES = ("image_to_video", "text_to_video")


def _runway_request_parameters(
    config: dict[str, Any],
    production: dict[str, Any],
    requested_seconds: float,
    generation_mode: str = "image_to_video",
    model: str | None = None,
) -> dict[str, Any]:
    """Resolve the exact model-native values that Runway will receive.

    This is intentionally independent from the Comfy/LTX profile.  The same
    resolved duration and ratio are used by cost approval, API generation and
    QA, preventing estimates and technical checks from describing a different
    request than the one actually billed.
    """
    runway = dict(config.get("runway", {}))
    model = str(model or production.get("model") or "")
    models = dict(runway.get("models", {}))
    spec = dict(models.get(model, {}))
    if not model or not spec:
        raise ValueError(
            f"Runwayモデル {model or '(未設定)'} が config.runway.models にありません"
        )
    supported_modes = tuple(
        str(value)
        for value in spec.get("generation_modes", _GENERATION_MODES)
    )
    if generation_mode not in supported_modes:
        raise ValueError(
            f"Runway {model} は現在 {generation_mode} に未対応です"
            f"（対応: {', '.join(supported_modes)}）"
        )
    caps = Capabilities(
        model=model,
        allowed_seconds=tuple(
            float(value) for value in spec.get("allowed_seconds", ())
        ),
        max_seconds=float(spec.get("max_seconds", 0.0)),
        resolutions=tuple(spec.get("resolutions", ())),
        supports_seed=bool(spec.get("supports_seed", True)),
        supports_negative_prompt=bool(
            spec.get("supports_negative_prompt", True)
        ),
        has_native_audio=bool(spec.get("has_native_audio", False)),
        cost_per_second_usd=float(spec.get("cost_per_second_usd", 0.0)),
        minimum_cost_usd=float(spec.get("minimum_cost_usd", 0.0)),
    )
    effective_seconds = caps.resolve_seconds(requested_seconds)
    common = {
        "model": model,
        "requested_seconds": requested_seconds,
        "actual_seconds": effective_seconds,
        "effective_seconds": effective_seconds,
    }
    resolution = str(spec.get("resolution") or "")
    if resolution:
        if caps.resolutions and resolution not in caps.resolutions:
            raise ValueError(
                f"Runway {model}: resolution={resolution} は許容値 "
                f"{caps.resolutions} にありません"
            )
        # Hailuo 3.0の2Kは入力画像の縦横比に追従するため、ここで架空の
        # width/heightへ変換しない。実寸は生成後にffprobeで記録する。
        return {**common, "resolution": resolution}

    ratio = str(spec.get("ratio") or runway.get("ratio") or "")
    if caps.resolutions and ratio not in caps.resolutions:
        raise ValueError(
            f"Runway {model}: ratio={ratio} は許容解像度 {caps.resolutions} にありません"
        )
    width, height = _ratio_dimensions(ratio)
    return {
        **common,
        "ratio": ratio,
        "width": width,
        "height": height,
    }


def support_video_creator(state: WorkflowState) -> dict[str, Any]:
    phase = "support_video_creator"
    direction = _result_data(state, "director")
    shots = list(direction.get("shots", []))
    if not shots:
        return deterministic._complete(
            state,
            phase,
            summary="DirectionPlanがないため生成Requestを作成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Directorのshotsが必要"],
        )

    config = state.get("config", {})
    production = config.get("production", {})
    backend = str(production.get("backend", "mock")).lower()
    profile_name = str(production.get("profile", "draft"))
    profile = dict(production.get("profiles", {}).get(profile_name, {}))
    constraints = production.get("model_constraints", {})
    frame_multiple = int(constraints.get("frame_multiple", 8))
    frame_offset = int(constraints.get("frame_offset", 1))
    max_frames = int(constraints.get("max_frames", 257))
    context = state.get("review_context", {}).get(phase, {})
    target_cut_id = (
        context.get("target_cut_id")
        or direction.get("targeted_revision_cut_id")
    )
    existing = dict(state.get("production_requests", {}))
    # target_cut_id は「前回の完全な結果があるときだけ」差分更新に使える。
    # Directorで1カットだけ差し戻すとその値がここまで引き継がれるが、
    # この工程が初回実行なら土台が無く、絞り込むと残りが未作成のまま落ちる
    # （run-d35ee139e1: Cut8のみ作られ、Cut1〜7が欠落）。
    target_cut_id = effective_target_cut_id(
        int(target_cut_id) if target_cut_id is not None else None,
        shots,
        existing,
    )
    if target_cut_id is None:
        existing = {}
    else:
        # Rebuild the targeted request from scratch. If model validation fails,
        # an old request must not remain eligible for another paid generation.
        existing.pop(str(target_cut_id), None)
    blocking: list[str] = []
    request_updates: dict[str, dict[str, Any]] = {}
    for shot in shots:
        cut_id = int(shot["id"])
        if target_cut_id is not None and cut_id != target_cut_id:
            continue
        seconds = float(shot["seconds"])
        generation_mode = str(
            shot.get("generation_mode") or "image_to_video"
        )
        selected_model = str(
            shot.get("model") or production.get("model") or ""
        )
        image_path = str((shot.get("asset") or {}).get("local_path") or "")
        # 生成方式ごとに必要な入力が異なる。方式と入力の不一致は、
        # 課金前のここで止める（画像が無いから text_to_video、という
        # 暗黙のフォールバックはしない）。
        if generation_mode not in _GENERATION_MODES:
            blocking.append(
                f"cut {cut_id}: 未知の generation_mode: {generation_mode}"
            )
        elif generation_mode == "text_to_video":
            if image_path:
                blocking.append(
                    f"cut {cut_id}: text_to_video に画像は指定できません: "
                    f"{image_path}"
                )
        elif backend != "mock" and (
            not image_path or not Path(image_path).exists()
        ):
            blocking.append(
                f"cut {cut_id}: ローカル入力画像が存在しない: "
                f"{image_path or '(empty)'}"
            )
        common_request = {
            "cut_id": cut_id,
            "backend": backend,
            "generation_mode": generation_mode,
            "media_requirement": shot.get("media_requirement"),
            "image_path": image_path,
            "positive_prompt": shot["positive_prompt"],
            "negative_prompt": shot.get("negative_prompt", ""),
            "camera_motion": shot.get("camera_motion", ""),
            "motion_intensity": shot.get("motion_intensity", "subtle"),
            "seed": _stable_seed(
                str(state.get("run_id", "")),
                cut_id,
                int(state.get("cut_attempts", {}).get(str(cut_id), 0)),
            ),
        }
        if backend == "runway":
            try:
                backend_parameters = _runway_request_parameters(
                    config,
                    production,
                    seconds,
                    generation_mode,
                    model=selected_model,
                )
            except (UnsupportedDurationError, ValueError) as exc:
                blocking.append(f"cut {cut_id}: {exc}")
                continue
            request_updates[str(cut_id)] = {
                **common_request,
                **backend_parameters,
                "request_contract": "runway_model_native",
            }
            continue

        # Comfy/mock keep the existing LTX profile and its 8n+1 frame rule.
        # These fields must never leak into the Runway request contract above.
        fps = int(profile.get("fps", 24))
        frames = _ltx_frame_count(
            seconds,
            fps,
            multiple=frame_multiple,
            offset=frame_offset,
        )
        if backend == "comfy" and frames > max_frames:
            blocking.append(
                f"cut {cut_id}: {frames} frames exceeds model max {max_frames}; "
                "Writer / StoryboardまたはDirectorでカットを分割してください"
            )
        width = int(profile.get("width", 576))
        height = int(profile.get("height", 384))
        pixel_load = width * height * frames
        cost_class = (
            "low"
            if pixel_load < 15_000_000
            else "medium"
            if pixel_load < 45_000_000
            else "high"
        )
        request_updates[str(cut_id)] = {
            **common_request,
            "workflow": str(
                config.get("comfy", {}).get(
                    "workflow_api_json",
                    "workflows/ltx_i2v_api.json",
                )
            ),
            "model_profile": profile_name,
            "width": width,
            "height": height,
            "frames": frames,
            "steps": int(profile.get("steps", 20)),
            "fps": fps,
            "requested_seconds": seconds,
            "actual_seconds": round(frames / fps, 4),
            "estimated_cost_class": cost_class,
            "request_contract": (
                "comfy_ltx" if backend == "comfy" else "local_frame_profile"
            ),
        }
    existing.update(request_updates)
    expected_ids = {str(int(shot["id"])) for shot in shots}
    missing = sorted(expected_ids - set(existing))
    if missing:
        blocking.append(
            "ProductionRequestがないカット: " + ", ".join(missing)
        )
    requests = [existing[key] for key in sorted(existing, key=int)]

    approved = {
        int(value) for value in state.get("approved_cut_ids", [])
    }
    if target_cut_id is not None:
        approved.discard(target_cut_id)
    queue = [
        int(request["cut_id"])
        for request in requests
        if int(request["cut_id"]) not in approved
    ]
    if target_cut_id is not None and target_cut_id in queue:
        queue.remove(target_cut_id)
        queue.insert(0, target_cut_id)

    production_artifacts = dict(
        state.get("production_artifacts", {})
    )
    cut_qa_results = dict(state.get("cut_qa_results", {}))
    if target_cut_id is not None:
        production_artifacts.pop(str(target_cut_id), None)
        cut_qa_results.pop(str(target_cut_id), None)

    # H2（重い生成を始める直前のゲート）で、実行全体の概算費用を人間へ提示する。
    # 以降の再生成では都度承認を求めず、上限超過の自動停止のみで守る。
    cost_estimate = _run_cost_estimate(state, backend, requests)
    blocking_or_warn = None
    if cost_estimate and cost_estimate.get("error"):
        # 尺がモデル上限を超える等、生成前に判明した問題は H2 で止める
        blocking.append(f"生成条件エラー: {cost_estimate['error']}")
    elif cost_estimate and cost_estimate.get("total_usd", 0) > 0:
        blocking_or_warn = (
            f"概算費用 ${cost_estimate['total_usd']:.2f}"
            f"（{cost_estimate['model']} / {len(requests)}カット）"
        )

    data = {
        "backend": backend,
        "profile_name": profile_name if backend != "runway" else None,
        "requests": requests,
        "request_count": len(requests),
        "targeted_revision_cut_id": target_cut_id,
        "cost_estimate": cost_estimate,
        "frame_rule": (
            {
                "multiple": frame_multiple,
                "offset": frame_offset,
                "max_frames": max_frames,
            }
            if backend != "runway"
            else None
        ),
        "request_contract": (
            "runway_model_native"
            if backend == "runway"
            else "comfy_ltx"
            if backend == "comfy"
            else "local_frame_profile"
        ),
    }
    update = deterministic._complete(
        state,
        phase,
        summary=(
            f"{len(requests)}カットのProductionRequestを構築"
            + (f" / {blocking_or_warn}" if blocking_or_warn else "")
        ),
        data=data,
        status="success" if not blocking else "error",
        confidence=1.0 if not blocking else 0.2,
        blocking_issues=blocking,
        warnings=[blocking_or_warn] if blocking_or_warn else None,
    )
    update.update(
        {
            "production_requests": existing,
            "production_queue": queue,
            "current_cut_id": (
                target_cut_id
                if target_cut_id is not None
                else state.get("current_cut_id")
            ),
            "approved_cut_ids": sorted(approved),
            "production_artifacts": production_artifacts,
            "cut_qa_results": cut_qa_results,
        }
    )
    return update
