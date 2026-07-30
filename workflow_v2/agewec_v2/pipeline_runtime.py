"""Runtime nodes for Phase 05.5 through Phase 10."""
from __future__ import annotations

import hashlib
import html
import json
import math
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import nodes as deterministic
from . import nodes_llm as llm_nodes
from .backends import ComfyClient, ComfyGenerationRequest
from .media_tools import (
    MediaToolError,
    concat_video_clips,
    decode_check,
    extract_representative_frames,
    generate_mock_video,
    image_to_video_clip,
    normalize_video_clip,
    probe_media,
)
from .state import WorkflowState


def _result_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _stable_seed(run_id: str, cut_id: int) -> int:
    digest = hashlib.sha256(f"{run_id}:{cut_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


def _ltx_frame_count(
    seconds: float,
    fps: int,
    *,
    multiple: int,
    offset: int,
) -> int:
    raw = max(1, round(seconds * fps))
    # Never round down: Phase 08 may trim a long clip, but it must not invent
    # missing frames when a generated clip is shorter than the storyboard.
    steps = max(0, math.ceil((raw - offset) / multiple))
    return max(offset, offset + steps * multiple)


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
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)

    existing = dict(state.get("production_requests", {}))
    if target_cut_id is None:
        existing = {}
    blocking: list[str] = []
    request_updates: dict[str, dict[str, Any]] = {}
    for shot in shots:
        cut_id = int(shot["id"])
        if target_cut_id is not None and cut_id != target_cut_id:
            continue
        seconds = float(shot["seconds"])
        fps = int(profile.get("fps", 24))
        frames = _ltx_frame_count(
            seconds,
            fps,
            multiple=frame_multiple,
            offset=frame_offset,
        )
        if frames > max_frames:
            blocking.append(
                f"cut {cut_id}: {frames} frames exceeds model max {max_frames}; "
                "Writer / StoryboardまたはDirectorでカットを分割してください"
            )
        image_path = str(shot.get("asset", {}).get("local_path") or "")
        if backend != "mock" and (
            not image_path or not Path(image_path).exists()
        ):
            blocking.append(
                f"cut {cut_id}: ローカル入力画像が存在しない: "
                f"{image_path or '(empty)'}"
            )
        pixel_load = (
            int(profile.get("width", 576))
            * int(profile.get("height", 384))
            * frames
        )
        cost_class = (
            "low"
            if pixel_load < 15_000_000
            else "medium"
            if pixel_load < 45_000_000
            else "high"
        )
        request_updates[str(cut_id)] = {
            "cut_id": cut_id,
            "backend": backend,
            "workflow": str(
                config.get("comfy", {}).get(
                    "workflow_api_json",
                    "workflows/ltx_i2v_api.json",
                )
            ),
            "model_profile": profile_name,
            "media_requirement": shot.get("media_requirement"),
            "image_path": image_path,
            "positive_prompt": shot["positive_prompt"],
            "negative_prompt": shot.get("negative_prompt", ""),
            "camera_motion": shot.get("camera_motion", ""),
            "motion_intensity": shot.get("motion_intensity", "subtle"),
            "width": int(profile.get("width", 576)),
            "height": int(profile.get("height", 384)),
            "frames": frames,
            "steps": int(profile.get("steps", 20)),
            "fps": fps,
            "seed": _stable_seed(str(state.get("run_id", "")), cut_id),
            "requested_seconds": seconds,
            "actual_seconds": round(frames / fps, 4),
            "estimated_cost_class": cost_class,
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

    data = {
        "backend": backend,
        "profile_name": profile_name,
        "requests": requests,
        "request_count": len(requests),
        "targeted_revision_cut_id": target_cut_id,
        "frame_rule": {
            "multiple": frame_multiple,
            "offset": frame_offset,
            "max_frames": max_frames,
        },
    }
    update = deterministic._complete(
        state,
        phase,
        summary=f"{len(requests)}カットのProductionRequestを構築",
        data=data,
        status="success" if not blocking else "error",
        confidence=1.0 if not blocking else 0.2,
        blocking_issues=blocking,
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


def _generate_comfy(
    state: WorkflowState,
    request: dict[str, Any],
) -> dict[str, Any]:
    config = state.get("config", {})
    comfy = dict(config.get("comfy", {}))
    comfy.update(config.get("production", {}).get("comfy", {}))
    workflow_path = deterministic.WORKFLOW_ROOT / str(
        comfy.get("workflow_api_json", "workflows/ltx_i2v_api.json")
    )
    client = ComfyClient(
        base_url=str(comfy.get("base_url", "http://127.0.0.1:8188")),
        workflow_path=workflow_path,
        input_mapping=comfy.get("inputs", {}),
        output_dir=deterministic._work_path(state, "production"),
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )
    return client.generate(
        ComfyGenerationRequest(
            image_path=request["image_path"],
            positive_prompt=request["positive_prompt"],
            negative_prompt=request["negative_prompt"],
            width=int(request["width"]),
            height=int(request["height"]),
            frames=int(request["frames"]),
            steps=int(request["steps"]),
            fps=int(request["fps"]),
            seed=int(request["seed"]),
            file_prefix=f"agewec_v2_cut_{int(request['cut_id']):02d}",
        )
    )


def image_video_production(state: WorkflowState) -> dict[str, Any]:
    phase = "image_video_production"
    requests = dict(state.get("production_requests", {}))
    approved = {int(value) for value in state.get("approved_cut_ids", [])}
    queue = [
        int(value)
        for value in state.get("production_queue", [])
        if int(value) not in approved
    ]
    current = state.get("current_cut_id")
    if current is None or int(current) in approved:
        current = queue[0] if queue else None
    if current is None:
        return deterministic._complete(
            state,
            phase,
            summary="生成待ちカットはありません",
            data={"queue_empty": True},
            confidence=1.0,
        )
    current = int(current)
    request = requests.get(str(current))
    if not request:
        return deterministic._complete(
            state,
            phase,
            summary=f"Cut {current}のProductionRequestがない",
            data={"cut_id": current, "issue_class": "generation_parameters"},
            status="error",
            confidence=0.0,
            blocking_issues=[f"Cut {current}のProductionRequestがない"],
        )

    attempts = dict(state.get("cut_attempts", {}))
    attempt = int(attempts.get(str(current), 0)) + 1
    attempts[str(current)] = attempt
    limits = state.get("config", {}).get("execution_limits", {})
    max_per_cut = int(limits.get("max_generation_attempts_per_cut", 2))
    max_total = int(limits.get("max_total_production_attempts", 20))
    total_attempts = sum(int(value) for value in attempts.values())
    if total_attempts > max_total:
        update = deterministic._complete(
            state,
            phase,
            summary="動画生成の全体上限に到達",
            data={
                "cut_id": current,
                "issue_class": "unknown",
                "attempt": attempt,
                "total_production_attempts": total_attempts,
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                f"全カット合計の最大生成回数{max_total}回を超過"
            ],
        )
        update.update(
            {
                "current_cut_id": current,
                "cut_attempts": attempts,
                "failed_cut_ids": sorted(
                    {
                        *state.get("failed_cut_ids", []),
                        current,
                    }
                ),
            }
        )
        return update
    if attempt > max_per_cut:
        update = deterministic._complete(
            state,
            phase,
            summary=f"Cut {current}が生成上限に到達",
            data={
                "cut_id": current,
                "issue_class": "unknown",
                "attempt": attempt,
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                f"Cut {current}: 最大生成回数{max_per_cut}回を超過"
            ],
        )
        update.update(
            {
                "current_cut_id": current,
                "cut_attempts": attempts,
                "failed_cut_ids": sorted(
                    {
                        *state.get("failed_cut_ids", []),
                        current,
                    }
                ),
            }
        )
        return update

    started = time.monotonic()
    output_dir = deterministic._work_path(state, "production")
    output_dir.mkdir(parents=True, exist_ok=True)
    backend = str(request["backend"])
    issues: list[str] = []
    generation: dict[str, Any] = {}
    output_path = ""
    try:
        if backend == "comfy":
            generation = _generate_comfy(state, request)
            output_path = str(generation["output_path"])
        elif backend == "mock":
            output_path = str(
                output_dir
                / f"cut_{current:02d}_attempt_{attempt:02d}.mp4"
            )
            generation = generate_mock_video(
                output_path,
                duration_seconds=float(request["actual_seconds"]),
                width=int(request["width"]),
                height=int(request["height"]),
                fps=int(request["fps"]),
                cut_id=current,
            )
        else:
            raise ValueError(f"Unsupported production backend: {backend}")
    except Exception as exc:
        issues.append(f"{type(exc).__name__}: {exc}")

    elapsed = round(time.monotonic() - started, 3)
    artifact = {
        "phase": phase,
        "cut_id": current,
        "kind": "video",
        "path": output_path,
        "backend": backend,
        "attempt": attempt,
        "elapsed_seconds": elapsed,
        "request": request,
        "generation": generation,
        "approved_for_final": False,
    }
    production_artifacts = dict(
        state.get("production_artifacts", {})
    )
    if not issues:
        production_artifacts[str(current)] = artifact
    cut_results = dict(state.get("cut_results", {}))
    cut_results[str(current)] = {
        **cut_results.get(str(current), {}),
        "production": artifact,
        "status": "generated" if not issues else "error",
    }
    update = deterministic._complete(
        state,
        phase,
        summary=(
            f"Cut {current}を{backend}で生成"
            if not issues
            else f"Cut {current}の生成に失敗"
        ),
        data={
            "cut_id": current,
            "backend": backend,
            "attempt": attempt,
            "total_production_attempts": total_attempts,
            "request": request,
            "artifact": artifact if not issues else None,
            "issue_class": "pass" if not issues else "runtime_transient",
        },
        artifacts=[artifact] if not issues else [],
        status="success" if not issues else "error",
        confidence=0.95 if not issues else 0.1,
        blocking_issues=issues,
    )
    update.update(
        {
            "current_cut_id": current,
            "cut_attempts": attempts,
            "production_artifacts": production_artifacts,
            "cut_results": cut_results,
            "generated_cut_ids": sorted(
                int(key) for key in production_artifacts
            ),
        }
    )
    return update


def cut_visual_qa(state: WorkflowState) -> dict[str, Any]:
    phase = "cut_visual_qa"
    current = state.get("current_cut_id")
    if current is None:
        return deterministic._complete(
            state,
            phase,
            summary="QA対象カットがありません",
            data={"verdict": "revise", "issue_class": "unknown"},
            status="error",
            confidence=0.0,
            blocking_issues=["current_cut_idが必要"],
        )
    current = int(current)
    artifact = state.get("production_artifacts", {}).get(str(current))
    request = state.get("production_requests", {}).get(str(current), {})
    issues: list[dict[str, Any]] = []
    technical: dict[str, Any] = {}
    frames: list[str] = []
    issue_class = "pass"
    try:
        if not artifact or not artifact.get("path"):
            raise MediaToolError("生成動画のArtifactがない")
        technical = probe_media(artifact["path"])
        decode_check(artifact["path"])
        expected = float(request.get("actual_seconds") or 0)
        delta = abs(float(technical["duration_seconds"]) - expected)
        tolerance = float(
            state.get("config", {})
            .get("qa", {})
            .get("duration_tolerance_seconds", 0.25)
        )
        if delta > tolerance:
            issue_class = "generation_parameters"
            issues.append(
                {
                    "code": "DURATION_MISMATCH",
                    "severity": "high",
                    "description": (
                        f"expected={expected:.3f}, "
                        f"actual={technical['duration_seconds']:.3f}"
                    ),
                    "evidence": [artifact["path"]],
                }
            )
        if (
            technical["width"] != int(request.get("width", 0))
            or technical["height"] != int(request.get("height", 0))
        ):
            issue_class = "generation_parameters"
            issues.append(
                {
                    "code": "RESOLUTION_MISMATCH",
                    "severity": "high",
                    "description": (
                        f"expected={request.get('width')}x"
                        f"{request.get('height')}, actual="
                        f"{technical['width']}x{technical['height']}"
                    ),
                    "evidence": [artifact["path"]],
                }
            )
        frames = extract_representative_frames(
            artifact["path"],
            deterministic._work_path(
                state,
                "qa",
                f"cut_{current:02d}",
            ),
            count=int(
                state.get("config", {})
                .get("qa", {})
                .get("representative_frame_count", 3)
            ),
        )
    except Exception as exc:
        issue_class = "runtime_transient"
        issues.append(
            {
                "code": "MEDIA_TECHNICAL_ERROR",
                "severity": "blocking",
                "description": f"{type(exc).__name__}: {exc}",
                "evidence": [
                    artifact.get("path")
                    if artifact
                    else "(missing artifact)"
                ],
            }
        )

    verdict = "pass" if not issues else "revise"
    route = {
        "pass": "next_cut",
        "runtime_transient": "image_video_production",
        "generation_parameters": "support_video_creator",
        "prompt_or_motion": "director",
        "source_asset": "asset_curator",
        "unknown": "human_review",
    }[issue_class]
    qa = {
        "cut_id": current,
        "verdict": verdict,
        "issue_class": issue_class,
        "issues": issues,
        "recommended_route": route,
        "recommended_feedback": (
            ""
            if verdict == "pass"
            else "; ".join(item["description"] for item in issues)
        ),
        "confidence": 0.98 if verdict == "pass" else 0.85,
        "technical": technical,
        "representative_frames": frames,
        "source_image": request.get("image_path"),
        "visual_evaluation": {
            "status": "not_evaluated",
            "reason": (
                "VLM connector is not configured; technical QA and "
                "representative-frame evidence are complete."
            ),
        },
    }
    cut_qa_results = dict(state.get("cut_qa_results", {}))
    cut_qa_results[str(current)] = qa
    cut_results = dict(state.get("cut_results", {}))
    cut_results[str(current)] = {
        **cut_results.get(str(current), {}),
        "qa": qa,
    }
    artifacts = [
        {
            "phase": phase,
            "cut_id": current,
            "kind": "qa_frame",
            "path": path,
        }
        for path in frames
    ]
    update = deterministic._complete(
        state,
        phase,
        summary=f"Cut {current} QA: {verdict} ({issue_class})",
        data=qa,
        artifacts=artifacts,
        status="success" if verdict == "pass" else "error",
        confidence=qa["confidence"],
        blocking_issues=[
            item["description"]
            for item in issues
            if item["severity"] in {"high", "blocking"}
        ],
        warnings=(
            ["視覚内容のVLM評価は未接続"]
            if verdict == "pass"
            else []
        ),
    )
    update.update(
        {
            "cut_qa_results": cut_qa_results,
            "cut_results": cut_results,
        }
    )
    return update


def commit_cut_qa(state: WorkflowState) -> dict[str, Any]:
    current = int(state.get("current_cut_id") or 0)
    qa = state.get("cut_qa_results", {}).get(str(current), {})
    approved = {int(value) for value in state.get("approved_cut_ids", [])}
    failed = {int(value) for value in state.get("failed_cut_ids", [])}
    queue = [
        int(value) for value in state.get("production_queue", [])
    ]
    artifacts = dict(state.get("production_artifacts", {}))
    context = dict(state.get("review_context", {}))
    feedback = dict(state.get("feedback", {}))
    route = qa.get("recommended_route", "human_review")

    if qa.get("verdict") == "pass":
        approved.add(current)
        failed.discard(current)
        queue = [cut_id for cut_id in queue if cut_id != current]
        if str(current) in artifacts:
            artifacts[str(current)] = {
                **artifacts[str(current)],
                "approved_for_final": True,
            }
        route = "next_cut" if queue else "sequence_qa"
        next_cut = queue[0] if queue else None
    else:
        failed.add(current)
        next_cut = current
        target_phase = {
            "image_video_production": "image_video_production",
            "support_video_creator": "support_video_creator",
            "director": "director",
            "asset_curator": "asset_curator",
        }.get(route)
        if target_phase:
            context[target_phase] = {
                "source_review": "cut_visual_qa",
                "target_cut_id": current,
                "correction_type": qa.get("issue_class", ""),
            }
            feedback[target_phase] = qa.get(
                "recommended_feedback",
                "",
            )
        if route == "asset_curator":
            context["director"] = {
                "source_review": "cut_visual_qa",
                "target_cut_id": current,
                "correction_type": "asset",
            }
        if route != "image_video_production":
            artifacts.pop(str(current), None)

    events = list(state.get("events", []))
    events.append(
        {
            "t": round(time.time(), 3),
            "type": "cut_qa_committed",
            "cut_id": current,
            "verdict": qa.get("verdict"),
            "route": route,
        }
    )
    return {
        "approved_cut_ids": sorted(approved),
        "failed_cut_ids": sorted(failed),
        "production_queue": queue,
        "current_cut_id": next_cut,
        "production_artifacts": artifacts,
        "review_context": context,
        "feedback": feedback,
        "cut_qa_route": route,
        "events": events,
    }


def sequence_visual_qa(state: WorkflowState) -> dict[str, Any]:
    phase = "visual_qa"
    storyboard = _result_data(state, "writer_storyboard")
    cuts = list(storyboard.get("cuts", []))
    expected = {int(cut["id"]) for cut in cuts}
    approved = {int(value) for value in state.get("approved_cut_ids", [])}
    missing = sorted(expected - approved)
    issues: list[dict[str, Any]] = []
    if missing:
        issues.append(
            {
                "code": "UNAPPROVED_CUTS",
                "description": f"未承認カット: {missing}",
                "affected_cut_ids": missing,
            }
        )
    requested_total = sum(float(cut["seconds"]) for cut in cuts)
    target = float(
        state.get("project", {}).get(
            "target_duration_seconds",
            requested_total,
        )
    )
    if abs(requested_total - target) > 0.25:
        issues.append(
            {
                "code": "TIMELINE_DURATION_MISMATCH",
                "description": (
                    f"storyboard={requested_total}, target={target}"
                ),
                "affected_cut_ids": [],
            }
        )
    data = {
        "verdict": "pass" if not issues else "revise",
        "scope": "pass" if not issues else "cut_range",
        "affected_cut_ids": sorted(
            {
                cut_id
                for issue in issues
                for cut_id in issue["affected_cut_ids"]
            }
        ),
        "issues": issues,
        "recommended_route": (
            "post_production"
            if not issues
            else "image_video_production"
        ),
        "recommended_feedback": (
            ""
            if not issues
            else "; ".join(issue["description"] for issue in issues)
        ),
        "confidence": 0.95 if not issues else 0.7,
        "sequence_readiness_checks": [
            "全カット承認済み",
            "カット順序",
            "予定尺",
            "素材と演出メタデータの連続性",
        ],
        "limitation": (
            "完成動画の最終テンポ・接続品質はPhase 08後に確認する"
        ),
    }
    return deterministic._complete(
        state,
        phase,
        summary=f"Sequence Readiness QA: {data['verdict']}",
        data=data,
        status="success" if not issues else "error",
        confidence=data["confidence"],
        blocking_issues=[
            issue["description"] for issue in issues
        ],
    )


def post_production(state: WorkflowState) -> dict[str, Any]:
    phase = "post_production"
    storyboard = _result_data(state, "writer_storyboard")
    cuts = list(storyboard.get("cuts", []))
    approved = {int(value) for value in state.get("approved_cut_ids", [])}
    artifacts = dict(state.get("production_artifacts", {}))
    config = state.get("config", {})
    post_config = config.get("post_production", {})
    width = int(post_config.get("width", 576))
    height = int(post_config.get("height", 384))
    fps = int(post_config.get("fps", 24))
    tolerance = float(
        post_config.get("duration_tolerance_seconds", 0.25)
    )
    output_dir = deterministic._work_path(state, "post")
    normalized_dir = output_dir / "normalized"
    output_dir.mkdir(parents=True, exist_ok=True)
    issues: list[str] = []
    timeline: list[dict[str, Any]] = []
    command_records: list[dict[str, Any]] = []
    normalized_paths: list[str] = []

    for order, cut in enumerate(cuts, start=1):
        cut_id = int(cut["id"])
        artifact = artifacts.get(str(cut_id))
        if cut_id not in approved:
            issues.append(f"cut {cut_id}: Phase 07未承認")
            continue
        if not artifact or not artifact.get("path"):
            issues.append(f"cut {cut_id}: 成果物がない")
            continue
        kind = artifact.get("kind")
        requirement = cut.get("media_requirement", "video_required")
        if requirement == "video_required" and kind != "video":
            issues.append(
                f"cut {cut_id}: video_requiredに動画が割り当てられていない"
            )
            continue
        source = Path(str(artifact["path"]))
        if not source.exists():
            issues.append(f"cut {cut_id}: ファイルが存在しない: {source}")
            continue
        destination = normalized_dir / f"cut_{cut_id:02d}.mp4"
        requested_seconds = float(cut["seconds"])
        try:
            if kind == "video":
                source_probe = probe_media(source)
                if (
                    float(source_probe["duration_seconds"])
                    < requested_seconds - tolerance
                ):
                    issues.append(
                        f"cut {cut_id}: 動画尺不足 "
                        f"{source_probe['duration_seconds']} < "
                        f"{requested_seconds}"
                    )
                    continue
                record = normalize_video_clip(
                    source,
                    destination,
                    duration_seconds=requested_seconds,
                    width=width,
                    height=height,
                    fps=fps,
                )
            else:
                record = image_to_video_clip(
                    source,
                    destination,
                    duration_seconds=requested_seconds,
                    width=width,
                    height=height,
                    fps=fps,
                )
            command_records.append(record)
            normalized_paths.append(str(destination))
            timeline.append(
                {
                    "order": order,
                    "cut_id": cut_id,
                    "source": str(source),
                    "normalized_source": str(destination),
                    "duration_seconds": requested_seconds,
                    "media_requirement": requirement,
                    "transition_to_next": "hard_cut",
                }
            )
        except Exception as exc:
            issues.append(
                f"cut {cut_id}: {type(exc).__name__}: {exc}"
            )

    expected_ids = {int(cut["id"]) for cut in cuts}
    actual_ids = {int(item["cut_id"]) for item in timeline}
    missing = sorted(expected_ids - actual_ids)
    if missing:
        issues.append(f"編集Manifestに不足カット: {missing}")

    final_path = output_dir / "final_video.mp4"
    technical: dict[str, Any] = {}
    if not issues:
        try:
            concat_record = concat_video_clips(
                normalized_paths,
                final_path,
                manifest_path=output_dir / "concat_manifest.txt",
            )
            command_records.append(concat_record)
            decode_check(final_path)
            technical = probe_media(final_path)
            expected_duration = float(
                state.get("project", {}).get(
                    "target_duration_seconds",
                    storyboard.get("total_seconds", 0),
                )
            )
            delta = abs(
                float(technical["duration_seconds"]) - expected_duration
            )
            technical.update(
                {
                    "status": (
                        "pass" if delta <= tolerance else "error"
                    ),
                    "expected_duration_seconds": expected_duration,
                    "duration_delta_seconds": round(delta, 4),
                    "issues": (
                        []
                        if delta <= tolerance
                        else [
                            f"最終尺差{delta:.3f}秒が許容値を超える"
                        ]
                    ),
                }
            )
            if technical["issues"]:
                issues.extend(technical["issues"])
        except Exception as exc:
            issues.append(f"{type(exc).__name__}: {exc}")

    edit_manifest = {
        "target_duration_seconds": state.get("project", {}).get(
            "target_duration_seconds"
        ),
        "timeline": timeline,
        "video_spec": {
            "width": width,
            "height": height,
            "fps": fps,
            "container": "mp4",
            "video_codec": "h264",
        },
        "subtitle_plan": {"status": "not_configured"},
        "narration_plan": {"status": "not_configured"},
        "bgm_plan": {"status": "not_configured"},
    }
    _json_write(output_dir / "edit_manifest.json", edit_manifest)
    _json_write(output_dir / "ffmpeg_commands.json", command_records)
    _json_write(output_dir / "technical_report.json", technical)
    _json_write(
        output_dir / "post_production_plan.json",
        {
            "edit_manifest": edit_manifest,
            "implementation": "ffmpeg_executed",
            "issues": issues,
        },
    )

    post_artifacts = [
        {
            "phase": phase,
            "kind": "edit_manifest",
            "path": str(output_dir / "edit_manifest.json"),
        },
        {
            "phase": phase,
            "kind": "technical_report",
            "path": str(output_dir / "technical_report.json"),
        },
    ]
    if final_path.exists():
        post_artifacts.insert(
            0,
            {
                "phase": phase,
                "kind": "final_video",
                "path": str(final_path),
            },
        )
    data = {
        "implementation": "ffmpeg_executed",
        "output_path": str(final_path) if final_path.exists() else None,
        "edit_manifest": edit_manifest,
        "technical_qa": technical,
        "issues": issues,
    }
    update = deterministic._complete(
        state,
        phase,
        summary=(
            f"最終動画を生成: {final_path.name}"
            if not issues
            else f"Post Productionで{len(issues)}件の問題"
        ),
        data=data,
        artifacts=post_artifacts,
        status="success" if not issues else "error",
        confidence=1.0 if not issues else 0.2,
        blocking_issues=issues,
    )
    if final_path.exists():
        update["final_output"] = str(final_path)
    return update


def review_board(state: WorkflowState) -> dict[str, Any]:
    mode = str(
        state.get("config", {})
        .get("review_board", {})
        .get("mode", "human_only")
    )
    if mode == "human_only":
        return deterministic._complete(
            state,
            "review_board",
            summary="AI Review Boardをスキップし、H3へ移行",
            data={
                "mode": mode,
                "verdict": "pass",
                "reason": state.get("config", {})
                .get("review_board", {})
                .get("skip_reason", "submission deadline priority"),
                "final_decision_required": True,
                "skipped_at": datetime.now(timezone.utc).isoformat(),
            },
            status="skipped",
            confidence=1.0,
            warnings=[
                "AI Review Board was skipped; human final review is mandatory."
            ],
        )
    if mode != "ai":
        return deterministic._complete(
            state,
            "review_board",
            summary=f"未知のReview Board mode: {mode}",
            data={"mode": mode, "verdict": "revise"},
            status="error",
            confidence=0.0,
            blocking_issues=[f"Unknown review_board.mode: {mode}"],
        )
    update = llm_nodes.review_board(state)
    post = _result_data(state, "post_production")
    technical = post.get("technical_qa", {})
    if technical.get("status") != "pass":
        result = dict(update["phase_results"]["review_board"])
        data = dict(result.get("data", {}))
        data["verdict"] = "revise"
        result["data"] = data
        result["status"] = "error"
        result["blocking_issues"] = [
            *result.get("blocking_issues", []),
            "Final Technical QAがpassではない",
        ]
        phase_results = dict(update["phase_results"])
        phase_results["review_board"] = result
        update["phase_results"] = phase_results
    return update


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decision_log(state: WorkflowState) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for event in state.get("events", []):
        decisions.append(
            {
                "timestamp": event.get("t"),
                "run_id": state.get("run_id"),
                "phase": event.get("phase"),
                "cut_id": event.get("cut_id"),
                "actor": (
                    "human"
                    if event.get("decided_by") == "human"
                    else "system"
                ),
                "action": event.get("type"),
                "decision": event.get("action") or event.get("summary"),
                "rationale": "",
                "evidence_refs": [],
            }
        )
    for review in state.get("reviews", []):
        decisions.append(
            {
                "timestamp": review.get("t"),
                "run_id": state.get("run_id"),
                "phase": review.get("phase"),
                "cut_id": review.get("target_cut_id"),
                "actor": review.get("decided_by"),
                "action": review.get("action"),
                "decision": review.get("feedback") or review.get("action"),
                "rationale": review.get("correction_type", ""),
                "evidence_refs": [],
            }
        )
    return decisions


def _process_markdown(state: WorkflowState, video_name: str) -> str:
    lines = [
        "# AGEWEC Production Process Report",
        "",
        f"- Run ID: `{state.get('run_id')}`",
        f"- Final video: `{video_name}`",
        f"- Target duration: "
        f"{state.get('project', {}).get('target_duration_seconds')} seconds",
        "",
        "## Workflow phases",
        "",
    ]
    for phase, result in state.get("phase_results", {}).items():
        lines.extend(
            [
                f"### {phase}",
                "",
                f"- Status: `{result.get('status')}`",
                f"- Summary: {result.get('summary', '')}",
                f"- Attempt: {result.get('attempt')}",
                f"- Confidence: {result.get('confidence')}",
                "",
            ]
        )
    lines.extend(["## Human / policy reviews", ""])
    for review in state.get("reviews", []):
        lines.append(
            f"- `{review.get('phase')}`: {review.get('action')} "
            f"by {review.get('decided_by')}; "
            f"{review.get('feedback', '')}"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "This report contains externally explainable decisions and evidence. "
            "It does not contain private chain-of-thought.",
            "",
        ]
    )
    return "\n".join(lines)


def _process_html(state: WorkflowState, video_name: str) -> str:
    cards = []
    for phase, result in state.get("phase_results", {}).items():
        payload = html.escape(
            json.dumps(
                deterministic._sanitized(result),
                ensure_ascii=False,
                indent=2,
            )
        )
        cards.append(
            "<details class='card'>"
            f"<summary>{html.escape(phase)} — "
            f"{html.escape(str(result.get('status')))}</summary>"
            f"<p>{html.escape(str(result.get('summary', '')))}</p>"
            f"<pre>{payload}</pre></details>"
        )
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGEWEC Process Report</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:40px auto;
padding:0 20px;background:#f5f5f2;color:#222}}
.hero,.card{{background:white;border:1px solid #ddd;border-radius:14px;
padding:18px;margin:12px 0}}summary{{font-weight:700;cursor:pointer}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f1eb;padding:14px}}
video{{width:100%;max-height:520px;background:#000}}
</style></head><body>
<section class="hero"><h1>AGEWEC Production Process</h1>
<p>Run ID: {html.escape(str(state.get('run_id')))}</p>
<video controls src="{html.escape(video_name)}"></video></section>
<h2>Phases</h2>{''.join(cards)}
<p>公開可能な判断理由と証跡のみを掲載し、内部Chain-of-Thoughtは含みません。</p>
</body></html>"""


def provenance_package(state: WorkflowState) -> dict[str, Any]:
    phase = "provenance"
    run_id = str(state.get("run_id") or f"run-{int(time.time())}")
    configured = (
        state.get("config", {})
        .get("paths", {})
        .get("submissions_dir", "submissions")
    )
    package = deterministic.WORKFLOW_ROOT / configured / run_id
    package.mkdir(parents=True, exist_ok=True)
    source_video = Path(str(state.get("final_output") or ""))
    if not source_video.exists():
        return deterministic._complete(
            state,
            phase,
            summary="最終動画がないため提出Packageを作成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["final_outputの動画が存在しない"],
        )
    final_video = package / "final_video.mp4"
    if source_video.resolve() != final_video.resolve():
        shutil.copy2(source_video, final_video)

    phase_results = deterministic._sanitized(
        state.get("phase_results", {})
    )
    provenance = {
        "run_id": run_id,
        "project": state.get("project", {}),
        "config": deterministic._sanitized(state.get("config", {})),
        "phase_results": phase_results,
        "reviews": state.get("reviews", []),
        "events": state.get("events", []),
        "artifacts": state.get("artifacts", []),
    }
    _json_write(package / "provenance.json", provenance)
    _json_write(
        package / "storyboard.json",
        _result_data(state, "writer_storyboard"),
    )
    _json_write(
        package / "direction_plan.json",
        _result_data(state, "director"),
    )
    _json_write(
        package / "review_summary.json",
        {
            "review_board": _result_data(state, "review_board"),
            "reviews": state.get("reviews", []),
        },
    )
    technical = _result_data(state, "post_production").get(
        "technical_qa",
        {},
    )
    _json_write(package / "technical_report.json", technical)
    decisions = _decision_log(state)
    (package / "decision_log.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in decisions
        ),
        encoding="utf-8",
    )
    markdown = _process_markdown(state, final_video.name)
    (package / "process_report.md").write_text(
        markdown,
        encoding="utf-8",
    )
    (package / "process_report.html").write_text(
        _process_html(state, final_video.name),
        encoding="utf-8",
    )

    qa_dir = package / "artifacts" / "qa"
    copied_qa: list[str] = []
    for result in state.get("cut_qa_results", {}).values():
        cut_id = int(result.get("cut_id", 0))
        for frame in result.get("representative_frames", []):
            source = Path(frame)
            if not source.exists():
                continue
            destination = qa_dir / f"cut_{cut_id:02d}_{source.name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_qa.append(str(destination.relative_to(package)))

    required = [
        final_video,
        package / "provenance.json",
        package / "technical_report.json",
        package / "process_report.html",
        package / "process_report.md",
        package / "decision_log.jsonl",
        package / "storyboard.json",
        package / "direction_plan.json",
        package / "review_summary.json",
    ]
    manifest_files = []
    for path in required:
        manifest_files.append(
            {
                "kind": path.stem,
                "path": str(path.relative_to(package)),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "run_id": run_id,
        "status": "ready_for_submission",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest_files,
        "qa_artifacts": copied_qa,
    }
    _json_write(package / "manifest.json", manifest)
    required.append(package / "manifest.json")
    missing = [str(path) for path in required if not path.exists()]
    status = "success" if not missing else "error"
    artifacts = [
        {
            "phase": phase,
            "kind": path.stem,
            "path": str(path),
        }
        for path in required
    ]
    update = deterministic._complete(
        state,
        phase,
        summary=f"提出Packageを生成: {package}",
        data={
            "package_dir": str(package),
            "final_video": str(final_video),
            "provenance": str(package / "provenance.json"),
            "process_report": str(package / "process_report.html"),
            "manifest": str(package / "manifest.json"),
            "status": (
                "ready_for_submission"
                if not missing
                else "error"
            ),
        },
        artifacts=artifacts,
        status=status,
        confidence=1.0 if not missing else 0.0,
        blocking_issues=missing,
    )
    update.update(
        {
            "final_output": str(final_video),
            "provenance_output": str(package / "provenance.json"),
            "process_report_output": str(
                package / "process_report.html"
            ),
            "submission_manifest": str(package / "manifest.json"),
        }
    )
    return update
