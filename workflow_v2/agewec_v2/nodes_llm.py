"""LLM-connected role nodes.

When llm.enabled is false these nodes delegate to the deterministic scaffolding in
nodes.py. When enabled, role decisions use RoleRunner while media/file operations
remain deterministic tools.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import nodes as deterministic
from .llm import LLMSettings, RoleRunner
from .state import WorkflowState


def _result_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _feedback(state: WorkflowState, phase: str) -> str:
    return state.get("feedback", {}).get(phase, "")


def _llm_settings(state: WorkflowState) -> LLMSettings:
    return LLMSettings.from_sources(state.get("config", {}))


def _with_llm_metadata(
    update: dict[str, Any],
    phase: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    phase_results = dict(update["phase_results"])
    result = dict(phase_results[phase])
    result["llm"] = metadata
    phase_results[phase] = result
    update["phase_results"] = phase_results
    return update


def _llm_error(
    state: WorkflowState,
    phase: str,
    exc: Exception,
) -> dict[str, Any]:
    return deterministic._complete(
        state,
        phase,
        summary=f"LLM実行失敗: {type(exc).__name__}",
        data={},
        status="error",
        confidence=0.0,
        blocking_issues=[f"{type(exc).__name__}: {exc}"],
    )


def _run_role(
    state: WorkflowState,
    *,
    phase: str,
    upstream: dict[str, Any],
    summary: Callable[[dict[str, Any]], str],
    fallback: Callable[[WorkflowState], dict[str, Any]],
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    try:
        settings = _llm_settings(state)
    except Exception as exc:
        return _llm_error(state, phase, exc)
    if not settings.enabled:
        return fallback(state)

    try:
        run = RoleRunner(state.get("config", {})).run(
            role=phase,
            upstream=upstream,
            feedback=_feedback(state, phase),
        )
        raw = run.output.model_dump(mode="json")
        data = transform(raw) if transform else raw
        update = deterministic._complete(
            state,
            phase,
            summary=summary(data),
            data=data,
            artifacts=artifacts,
            confidence=float(data.get("confidence", 0.9)),
            warnings=warnings,
        )
        return _with_llm_metadata(update, phase, run.metadata)
    except Exception as exc:
        if settings.strict_mode:
            return _llm_error(state, phase, exc)
        update = fallback(state)
        result = dict(update["phase_results"][phase])
        result["warnings"] = list(result.get("warnings", [])) + [
            f"LLM失敗のため決定的フォールバックを使用: {type(exc).__name__}: {exc}"
        ]
        result["llm"] = {
            "provider": settings.provider,
            "model": settings.model,
            "fallback": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        phase_results = dict(update["phase_results"])
        phase_results[phase] = result
        update["phase_results"] = phase_results
        return update


def executive_producer(state: WorkflowState) -> dict[str, Any]:
    project = state.get("project", {})
    return _run_role(
        state,
        phase="executive_producer",
        upstream={
            "project": project,
            "system_capabilities": {
                "orchestrator": "LangGraph",
                "media_backend": state.get("config", {})
                .get("production", {})
                .get("backend", "mock"),
                "review_modes": ["always", "on_exception", "never"],
            },
        },
        summary=lambda data: f"{data['deliverable']}の制作方針をLLMが定義",
        fallback=deterministic.executive_producer,
    )


def creative_director(state: WorkflowState) -> dict[str, Any]:
    brief = _result_data(state, "executive_producer")
    if not brief:
        return deterministic._complete(
            state,
            "creative_director",
            summary="上流ProjectBriefがないため実行不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["executive_producerの有効な出力が必要"],
        )
    return _run_role(
        state,
        phase="creative_director",
        upstream={
            "project": state.get("project", {}),
            "project_brief": brief,
        },
        summary=lambda data: f"コンセプト「{data['title']}」をLLMが策定",
        fallback=deterministic.creative_director,
    )


def writer_storyboard(state: WorkflowState) -> dict[str, Any]:
    brief = _result_data(state, "executive_producer")
    concept = _result_data(state, "creative_director")
    if not brief or not concept:
        return deterministic._complete(
            state,
            "writer_storyboard",
            summary="上流企画が不足しているため実行不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["ProjectBriefとCreativeConceptが必要"],
        )

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        target = float(
            state.get("project", {}).get("target_duration_seconds", 30)
        )
        if abs(float(data["total_seconds"]) - target) > 1.0:
            raise ValueError(
                f"storyboard duration {data['total_seconds']} "
                f"does not match target {target}"
            )
        return data

    return _run_role(
        state,
        phase="writer_storyboard",
        upstream={
            "project": state.get("project", {}),
            "project_brief": brief,
            "creative_concept": concept,
        },
        summary=lambda data: (
            f"{len(data['cuts'])}カット、約{data['total_seconds']}秒をLLMが構成"
        ),
        fallback=deterministic.writer_storyboard,
        transform=transform,
    )


def _asset_candidates(state: WorkflowState) -> list[dict[str, Any]]:
    award = state.get("project", {}).get("target_award", "夜景賞")
    genre = deterministic.AWARD_GENRES.get(award)
    catalog = deterministic._load_catalog()
    photos = catalog.get("photos", [])
    if genre:
        photos = [photo for photo in photos if genre in photo.get("genres", [])]
    candidates = []
    for index, photo in enumerate(photos[:24], start=1):
        candidates.append(
            {
                "asset_id": f"asset-{index:03d}",
                "title": photo.get("title", ""),
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": photo.get("genres", []),
                "areas": photo.get("areas", []),
                "local_path": deterministic._local_asset_path(
                    photo.get("image_url", "")
                ),
                "rights_status": "review_required",
                "rights_note": "公式配布元の最新利用条件を提出前に確認する",
            }
        )
    return candidates


def asset_curator(state: WorkflowState) -> dict[str, Any]:
    storyboard = _result_data(state, "writer_storyboard")
    if not storyboard:
        return deterministic._complete(
            state,
            "asset_curator",
            summary="絵コンテがないため素材選定不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Storyboardが必要"],
        )
    candidates = _asset_candidates(state)

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        candidate_map = {item["asset_id"]: item for item in candidates}
        valid_cut_ids = {int(cut["id"]) for cut in storyboard["cuts"]}
        selected = []
        invalid = []
        for selection in data["selections"]:
            asset_id = selection["asset_id"]
            cut_id = int(selection["cut_id"])
            if asset_id not in candidate_map or cut_id not in valid_cut_ids:
                invalid.append(f"cut={cut_id}, asset={asset_id}")
                continue
            selected.append(
                {
                    **candidate_map[asset_id],
                    "cut_id": cut_id,
                    "selection_reason": selection["reason"],
                    "rights_risk": selection["rights_risk"],
                }
            )
        if invalid:
            raise ValueError(
                "LLM selected unknown candidate/cut IDs: " + ", ".join(invalid)
            )
        if not selected:
            raise ValueError("LLM selected no real asset candidates")
        missing_cuts = sorted(
            valid_cut_ids - {int(item["cut_id"]) for item in selected}
        )
        return {
            "catalog_source": (
                deterministic._load_catalog().get("source")
            ),
            "available_candidate_count": len(candidates),
            "selected_assets": selected,
            "missing_requirements": data.get("missing_requirements", []),
            "unassigned_cut_ids": missing_cuts,
            "rights_check_required": True,
        }

    return _run_role(
        state,
        phase="asset_curator",
        upstream={
            "project": state.get("project", {}),
            "storyboard": storyboard,
            "available_asset_candidates": candidates,
            "selection_rule": "Use only supplied asset_id values.",
        },
        summary=lambda data: (
            f"LLMが公式素材{len(data['selected_assets'])}件を選定"
        ),
        fallback=deterministic.asset_curator,
        transform=transform,
        warnings=["素材利用条件は提出前に人間が最終確認する"],
    )


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
    config = state.get("config", {})
    production = config.get("production", {})
    active_profile = production.get("profile", "draft")
    profiles = production.get("profiles", {})

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        cut_map = {int(cut["id"]): cut for cut in storyboard["cuts"]}
        asset_map = {
            item["asset_id"]: item
            for item in assets.get("selected_assets", [])
        }
        shots = []
        invalid = []
        for direction in data["shots"]:
            cut_id = int(direction["cut_id"])
            asset_id = direction["asset_id"]
            profile_name = direction["generation_profile"]
            if (
                cut_id not in cut_map
                or asset_id not in asset_map
                or profile_name not in profiles
            ):
                invalid.append(
                    f"cut={cut_id}, asset={asset_id}, profile={profile_name}"
                )
                continue
            shots.append(
                {
                    **cut_map[cut_id],
                    "asset": asset_map[asset_id],
                    "positive_prompt": direction["positive_prompt"],
                    "negative_prompt": direction["negative_prompt"],
                    "camera_motion": direction["camera_motion"],
                    "generation_profile_name": profile_name,
                    "generation_profile": profiles[profile_name],
                }
            )
        if invalid:
            raise ValueError(
                "Director returned invalid IDs/profiles: " + ", ".join(invalid)
            )
        if len(shots) != len(cut_map):
            raise ValueError("Director must return exactly one shot per cut")
        return {
            "profile_name": active_profile,
            "shots": shots,
            "continuity_checks": data["continuity_checks"],
        }

    return _run_role(
        state,
        phase="director",
        upstream={
            "project": state.get("project", {}),
            "creative_concept": concept,
            "storyboard": storyboard,
            "asset_selection": assets,
            "available_generation_profiles": list(profiles),
            "preferred_generation_profile": active_profile,
        },
        summary=lambda data: (
            f"LLMが{len(data['shots'])}カットの生成・演出指示を確定"
        ),
        fallback=deterministic.director,
        transform=transform,
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
            "project": state.get("project", {}),
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
            "project": state.get("project", {}),
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
            "project": state.get("project", {}),
            "project_brief": _result_data(state, "executive_producer"),
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
