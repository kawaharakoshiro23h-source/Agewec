"""Phase 07B sequence readiness checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fallbacks import common as deterministic
from ..state import WorkflowState

from .common import _approved_project_value, _result_data

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
    production_artifacts = dict(state.get("production_artifacts", {}))
    missing_artifacts: list[int] = []
    missing_files: list[int] = []
    for cut_id in sorted(expected):
        artifact = production_artifacts.get(str(cut_id))
        if not isinstance(artifact, dict):
            missing_artifacts.append(cut_id)
            continue
        raw_path = str(artifact.get("path") or "")
        if not raw_path or not Path(raw_path).is_file():
            missing_files.append(cut_id)
    if missing_artifacts:
        issues.append(
            {
                "code": "MISSING_PRODUCTION_ARTIFACTS",
                "description": (
                    f"生成成果物情報がないカット: {missing_artifacts}"
                ),
                "affected_cut_ids": missing_artifacts,
            }
        )
    if missing_files:
        issues.append(
            {
                "code": "MISSING_PRODUCTION_FILES",
                "description": (
                    f"生成動画ファイルが存在しないカット: {missing_files}"
                ),
                "affected_cut_ids": missing_files,
            }
        )
    requested_total = sum(float(cut["seconds"]) for cut in cuts)
    target = float(
        _approved_project_value(
            state,
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
            "全カットの生成成果物とファイルの実在",
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
