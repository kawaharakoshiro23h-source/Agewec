"""LLM role nodes plus guarded real-media production."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import nodes as deterministic
from . import nodes_llm as llm_nodes
from .backends import ComfyClient, ComfyGenerationRequest


executive_producer = llm_nodes.executive_producer
creative_director = llm_nodes.creative_director
writer_storyboard = llm_nodes.writer_storyboard
asset_curator = llm_nodes.asset_curator
director = llm_nodes.director
visual_qa = llm_nodes.visual_qa
post_production = llm_nodes.post_production
review_board = llm_nodes.review_board
provenance = llm_nodes.provenance


def select_video_shots(
    shots: list[dict[str, Any]],
    *,
    max_video_cuts: int,
    requested_cut_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    videos = [shot for shot in shots if shot.get("media_strategy") == "video"]
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


def image_video_production(state: dict[str, Any]) -> dict[str, Any]:
    config = state.get("config", {})
    production = config.get("production", {})
    if str(production.get("backend", "mock")).lower() != "comfy":
        return llm_nodes.image_video_production(state)

    shots = list(
        llm_nodes._result_data(state, "director").get("shots", [])
    )
    if not shots:
        return deterministic._complete(
            state,
            "image_video_production",
            summary="Directorのshotsがないため生成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Directorのshotsが必要"],
        )

    max_video_cuts = int(production.get("max_video_cuts_per_run", 1))
    selected, deferred = select_video_shots(
        shots,
        max_video_cuts=max_video_cuts,
        requested_cut_ids=production.get("video_cut_ids"),
    )
    selected_ids = {str(shot.get("id")) for shot in selected}
    deferred_ids = {str(shot.get("id")) for shot in deferred}

    comfy = dict(config.get("comfy", {}))
    comfy.update(production.get("comfy", {}))
    workflow_path = deterministic.WORKFLOW_ROOT / str(
        comfy.get(
            "workflow_api_json",
            comfy.get("workflow", "workflows/ltx_i2v_api.json"),
        )
    )
    client = ComfyClient(
        base_url=str(comfy.get("base_url", "http://127.0.0.1:8188")),
        workflow_path=workflow_path,
        input_mapping=comfy.get(
            "inputs",
            comfy.get("input_mapping", {}),
        ),
        output_dir=deterministic._work_path(state, "production"),
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )

    artifacts: list[dict[str, Any]] = []
    issues: list[str] = []
    generation_runs: list[dict[str, Any]] = []

    for shot in shots:
        raw_cut_id = shot.get("id")
        cut_id = str(raw_cut_id)
        source_path = str(shot.get("asset", {}).get("local_path") or "")
        source = Path(source_path).expanduser() if source_path else None
        if not source or not source.exists():
            issues.append(
                f"cut {cut_id}: 入力画像がない: {source_path or '(empty)'}"
            )
            continue

        if cut_id in deferred_ids:
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": raw_cut_id,
                    "kind": "source_image_fallback",
                    "path": str(source.resolve()),
                    "backend": "source",
                    "generation_deferred": True,
                    "reason": "max_video_cuts_per_run",
                }
            )
            continue

        if cut_id not in selected_ids:
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": raw_cut_id,
                    "kind": "source_image",
                    "path": str(source.resolve()),
                    "backend": "source",
                }
            )
            continue

        profile = shot.get("generation_profile", {})
        request = ComfyGenerationRequest(
            image_path=str(source.resolve()),
            positive_prompt=str(shot.get("positive_prompt") or ""),
            negative_prompt=str(
                shot.get("negative_prompt")
                or comfy.get("default_negative_prompt", "")
            ),
            width=int(profile.get("width", 576)),
            height=int(profile.get("height", 384)),
            frames=int(profile.get("frames", 49)),
            steps=int(profile.get("steps", 20)),
            fps=int(profile.get("fps", 24)),
            seed=int(shot.get("seed") or time.time_ns() % 2_147_483_647),
            file_prefix=f"agewec_v2_cut_{cut_id}",
        )
        try:
            result = client.generate(request)
            generation_runs.append(result)
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": raw_cut_id,
                    "kind": "video",
                    "path": result["output_path"],
                    "backend": "comfy",
                    "prompt_id": result["prompt_id"],
                    "elapsed_seconds": result["elapsed_seconds"],
                }
            )
        except Exception as exc:
            issues.append(f"cut {cut_id}: {type(exc).__name__}: {exc}")
            if not bool(production.get("continue_on_cut_error", False)):
                break

    generated_ids = [
        artifact["cut_id"]
        for artifact in artifacts
        if artifact["kind"] == "video"
    ]
    data = {
        "backend": "comfy",
        "generated_video_cut_ids": generated_ids,
        "deferred_video_cut_ids": [
            shot.get("id") for shot in deferred
        ],
        "max_video_cuts_per_run": max_video_cuts,
        "workflow_path": str(workflow_path),
        "generation_runs": generation_runs,
    }
    return deterministic._complete(
        state,
        "image_video_production",
        summary=(
            f"Comfyで{len(generated_ids)}動画カットを生成し、"
            f"{len(deferred)}カットは静止画フォールバック"
        ),
        data=data,
        artifacts=artifacts,
        status="success" if not issues else "error",
        confidence=0.88 if not issues else 0.2,
        blocking_issues=issues,
    )
