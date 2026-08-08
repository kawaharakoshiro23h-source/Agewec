"""Phase 08 post production and final review-board preparation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import nodes as deterministic
from .. import nodes_llm as llm_nodes
from ..media_tools import (
    concat_video_clips,
    decode_check,
    image_to_video_clip,
    normalize_video_clip,
    probe_media,
)
from ..state import WorkflowState

from .common import (
    _approved_project_value, _json_write, _result_data,
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
    output_dir = deterministic._work_path(state, "final")
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
                _approved_project_value(
                    state,
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
        "target_duration_seconds": _approved_project_value(
            state,
            "target_duration_seconds",
            storyboard.get("total_seconds"),
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
        # クリップ側の音声は正規化時に必ず除去している（-an）。
        # 音は最終工程でBGM/ナレーションを一本だけ乗せる方針。
        "audio_policy": {
            "clip_audio": "stripped",
            "reason": (
                "クラウドモデルはクリップ毎に音声を生成するため、"
                "連結時に環境音が切り替わるのを防ぐ"
            ),
            "final_audio": "single_track_added_later",
        },
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

