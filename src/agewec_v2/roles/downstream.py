"""LLM-assisted legacy downstream review roles."""
from __future__ import annotations

import json
from typing import Any

from ..fallbacks import legacy_media as deterministic
from ..state import WorkflowState

from .common import (
    _approved_project_brief, _llm_error, _llm_settings, _result_data,
    _run_role,
)

def image_video_production(state: WorkflowState) -> dict[str, Any]:
    director_data = _result_data(state, "director")
    if not director_data.get("shots"):
        return deterministic._complete(
            state,
            "image_video_production",
            summary="有効な演出設計がないため生成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Directorのshotsが必要"],
        )
    return deterministic.image_video_production(state)


def visual_qa(state: WorkflowState) -> dict[str, Any]:
    technical_update = deterministic.visual_qa(state)
    technical_result = technical_update["phase_results"]["visual_qa"]
    try:
        settings = _llm_settings(state)
    except Exception as exc:
        return _llm_error(state, "visual_qa", exc)
    if not settings.enabled or technical_result.get("blocking_issues"):
        return technical_update

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "checked_artifacts": len(
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
            "checks": ["ファイル存在", "ゼロバイトでないこと", "LLM metadata review"],
        }

    return _run_role(
        state,
        phase="visual_qa",
        upstream={
            "project_brief": _approved_project_brief(state),
            "storyboard": _result_data(state, "writer_storyboard"),
            "direction_plan": _result_data(state, "director"),
            "production_result": _result_data(
                state, "image_video_production"
            ),
            "artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
            "deterministic_checks": technical_result,
            "evidence_limit": (
                "No decoded video frames are supplied in this text-only pass."
            ),
        },
        summary=lambda data: (
            f"LLM Visual QA: {data['verdict']} → {data['route']}"
        ),
        fallback=lambda _: technical_update,
        transform=transform,
        warnings=["現在のLLM QAは映像フレームではなくメタデータ評価"],
    )


def post_production(state: WorkflowState) -> dict[str, Any]:
    """[LEGACY 未使用] 旧・LLMで編集計画のみ作る版（`ffmpeg_pending`）。

    本番は `phases.post_production`（FFmpegで実結合＝`ffmpeg_executed`）。
    互換のため残置。新しい実装はこちらに追加しないこと。
    """
    production_result = _result_data(state, "image_video_production")
    if not production_result:
        return deterministic._complete(
            state,
            "post_production",
            summary="Production成果物がないため編集計画を作成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Image/Video Production成果物が必要"],
        )

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        output_dir = deterministic._work_path(state, "post")
        output_dir.mkdir(parents=True, exist_ok=True)
        plan_path = output_dir / "post_production_plan.json"
        payload = {
            **data,
            "implementation": "ffmpeg_pending",
            "source_artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
        }
        plan_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload["plan_path"] = str(plan_path)
        return payload

    return _run_role(
        state,
        phase="post_production",
        upstream={
            "project_brief": _approved_project_brief(state),
            "storyboard": _result_data(state, "writer_storyboard"),
            "visual_qa": _result_data(state, "visual_qa"),
            "media_artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
        },
        summary=lambda data: (
            f"LLMが{len(data['operations'])}工程の編集計画を作成"
        ),
        fallback=deterministic.post_production,
        transform=transform,
        warnings=["FFmpeg実行は次の実装段階"],
    )


def review_board(state: WorkflowState) -> dict[str, Any]:
    post = _result_data(state, "post_production")
    if not post:
        return deterministic._complete(
            state,
            "review_board",
            summary="Post Production成果物がないため評価不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Post Production成果物が必要"],
        )
    return _run_role(
        state,
        phase="review_board",
        upstream={
            "project_brief": _approved_project_brief(state),
            "creative_concept": _result_data(state, "creative_director"),
            "storyboard": _result_data(state, "writer_storyboard"),
            "asset_manifest": _result_data(state, "asset_curator"),
            "visual_qa": _result_data(state, "visual_qa"),
            "post_production": post,
            "evidence_limit": (
                "Rendered final MP4 inspection is not yet available."
            ),
        },
        summary=lambda data: (
            f"LLM Review Board: {data['average']}/5、{data['verdict']}"
        ),
        fallback=deterministic.review_board,
    )


def provenance(state: WorkflowState) -> dict[str, Any]:
    return deterministic.provenance(state)
