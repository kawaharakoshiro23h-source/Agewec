"""Legacy deterministic media nodes retained for fallback compatibility."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..backends import ComfyClient, ComfyGenerationRequest
from ..paths import runtime_paths
from ..state import WorkflowState

from .common import (
    _complete, _cut_path, _sanitized, _work_path,
)

def _mock_production(
    state: WorkflowState,
    shots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    output_dir = _work_path(state, "production")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for shot in shots:
        path = output_dir / f"shot_{shot['id']:02d}_request.json"
        path.write_text(
            json.dumps(shot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "phase": "image_video_production",
                "cut_id": shot["id"],
                "kind": "generation_request",
                "path": str(path),
                "backend": "mock",
            }
        )
    return artifacts, []


def _comfy_production(
    state: WorkflowState,
    shots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    config = state.get("config", {})
    comfy = config.get("comfy", {})
    workflow_path = runtime_paths(config).resolve_workflow(
        comfy.get("workflow_api_json", "workflows/ltx_i2v_api.json")
    )
    client = ComfyClient(
        base_url=comfy.get("base_url", "http://127.0.0.1:8188"),
        workflow_path=workflow_path,
        input_mapping=comfy.get("inputs", {}),
        output_dir=_work_path(state, "production"),
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )
    artifacts: list[dict[str, Any]] = []
    issues: list[str] = []
    for shot in shots:
        requires_video = (
            shot.get("media_requirement") == "video_required"
            or shot.get("media_strategy") == "video"
        )
        if not requires_video:
            # text_to_video のカットでは asset が None になりうる。
            # `.get("asset", {})` は値が None のとき None を返すので不可。
            source = (shot.get("asset") or {}).get("local_path")
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": shot["id"],
                    "kind": "source_image",
                    "path": source,
                    "backend": "source",
                }
            )
            if not source:
                issues.append(f"cut {shot['id']}: 静止画素材がローカルにない")
            continue
        # text_to_video のカットでは asset が None になりうる。
        # `.get("asset", {})` は値が None のとき None を返すので不可。
        source = (shot.get("asset") or {}).get("local_path")
        if not source:
            issues.append(f"cut {shot['id']}: ComfyUI入力画像がない")
            continue
        production = config.get("production", {})
        profile_name = str(production.get("profile", "draft"))
        profile = (
            shot.get("generation_profile")
            or production.get("profiles", {}).get(profile_name, {})
        )
        try:
            result = client.generate(
                ComfyGenerationRequest(
                    image_path=source,
                    positive_prompt=shot["positive_prompt"],
                    negative_prompt=shot.get("negative_prompt", ""),
                    width=int(profile.get("width", 576)),
                    height=int(profile.get("height", 384)),
                    frames=int(profile.get("frames", 49)),
                    steps=int(profile.get("steps", 20)),
                    fps=int(profile.get("fps", 24)),
                    seed=int(time.time_ns() % 2_147_483_647),
                    file_prefix=f"agewec_v2_cut_{shot['id']:02d}",
                )
            )
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": shot["id"],
                    "kind": "video",
                    "path": result["output_path"],
                    "backend": "comfy",
                    "generation": result,
                }
            )
        except Exception as exc:
            issues.append(f"cut {shot['id']}: {type(exc).__name__}: {exc}")
    return artifacts, issues


def image_video_production(state: WorkflowState) -> dict[str, Any]:
    phase = "image_video_production"
    shots = state["phase_results"]["director"]["data"]["shots"]
    backend = state.get("config", {}).get("production", {}).get("backend", "mock")
    if backend == "comfy":
        artifacts, issues = _comfy_production(state, shots)
    else:
        artifacts, issues = _mock_production(state, shots)
    data = {
        "backend": backend,
        "requested_shots": len(shots),
        "completed_artifacts": len(artifacts),
    }
    return _complete(
        state,
        phase,
        summary=f"{backend}バックエンドで{len(artifacts)}件の成果物を作成",
        data=data,
        artifacts=artifacts,
        status="success" if not issues else "error",
        confidence=0.9 if not issues else 0.4,
        blocking_issues=issues,
        warnings=["mockは実メディアを生成しない"] if backend == "mock" else [],
    )


def visual_qa(state: WorkflowState) -> dict[str, Any]:
    phase = "visual_qa"
    production = state["phase_results"]["image_video_production"]
    artifacts = production.get("artifacts", [])
    missing = []
    empty = []
    for artifact in artifacts:
        raw_path = artifact.get("path")
        if not raw_path:
            missing.append(f"cut {artifact.get('cut_id')}: pathなし")
            continue
        path = Path(raw_path)
        if not path.exists():
            missing.append(str(path))
        elif path.is_file() and path.stat().st_size == 0:
            empty.append(str(path))
    issues = [f"成果物が存在しない: {item}" for item in missing]
    issues.extend(f"成果物が空: {item}" for item in empty)
    route = "image_video_production" if issues else "post_production"
    qa = {
        "route": route,
        "checked_artifacts": len(artifacts),
        "checks": ["ファイル存在", "ゼロバイトでないこと"],
        "future_checks": [
            "ffprobeによるduration/fps/codec",
            "VLMによる映像破綻と指示整合性",
        ],
    }
    return _complete(
        state,
        phase,
        summary=(
            "機械検査を通過"
            if not issues
            else f"機械検査で{len(issues)}件の問題を検出"
        ),
        data=qa,
        status="success" if not issues else "error",
        confidence=0.9 if not issues else 0.3,
        blocking_issues=issues,
        warnings=["内容品質のVLM検査は未実装"],
    )


def post_production(state: WorkflowState) -> dict[str, Any]:
    """[LEGACY 未使用] 旧・編集計画のみ版（`ffmpeg_pending` を返す）。

    本番は `phases.post_production`（FFmpegで実結合＝`ffmpeg_executed`）。
    互換のため残置。新しい実装はこちらに追加しないこと。
    """
    phase = "post_production"
    production_artifacts = state["phase_results"]["image_video_production"].get(
        "artifacts", []
    )
    output_dir = _work_path(state, "post")
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "post_production_plan.json"
    plan = {
        "timeline": [
            {
                "cut_id": artifact.get("cut_id"),
                "source": artifact.get("path"),
                "kind": artifact.get("kind"),
            }
            for artifact in production_artifacts
        ],
        "operations": [
            "全カットを共通解像度・fpsへ正規化",
            "静止画にパン・ズームを適用",
            "動画を絵コンテ順に結合",
            "字幕・ナレーション・BGMをミックス",
        ],
        "implementation": "ffmpeg_pending",
    }
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = {
        "phase": phase,
        "kind": "post_production_plan",
        "path": str(plan_path),
    }
    return _complete(
        state,
        phase,
        summary="タイムラインとFFmpeg後処理計画を生成",
        data=plan,
        artifacts=[artifact],
        confidence=0.8,
        warnings=["実MP4のFFmpeg結合は未実装"],
    )


def review_board(state: WorkflowState) -> dict[str, Any]:
    phase = "review_board"
    post = state["phase_results"]["post_production"]
    asset = state["phase_results"]["asset_curator"]["data"]
    scores = {
        "concept_consistency": 4,
        "story_structure": 4,
        "asset_traceability": 4 if asset.get("selected_assets") else 1,
        "technical_completion": (
            2 if post["data"].get("implementation") == "ffmpeg_pending" else 5
        ),
    }
    average = round(sum(scores.values()) / len(scores), 2)
    board = {
        "rubric_scores_5": scores,
        "average": average,
        "verdict": "pass",
        "recommendations": [
            "提出前に素材利用条件を最終確認する",
            "FFmpeg実装後に技術完成度を再採点する",
        ],
    }
    return _complete(
        state,
        phase,
        summary=f"Review Board評価 {average}/5、最終承認へ",
        data=board,
        confidence=0.78,
        warnings=board["recommendations"],
    )


# 秘匿すべきキー。部分一致で判定する。

def provenance(state: WorkflowState) -> dict[str, Any]:
    phase = "provenance"
    path = runtime_paths(state.get("config", {})).provenance_file
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": state.get("run_id"),
        "project": state.get("project"),
        "config": _sanitized(state.get("config", {})),
        "phase_results": state.get("phase_results", {}),
        "reviews": state.get("reviews", []),
        "events": state.get("events", []),
        "artifacts": state.get("artifacts", []),
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = {
        "phase": phase,
        "kind": "provenance",
        "path": str(path),
    }
    update = _complete(
        state,
        phase,
        summary=f"証跡を{path.name}へ保存",
        data={"record_path": str(path)},
        artifacts=[artifact],
        confidence=1.0,
    )
    update["final_output"] = str(path)
    return update
