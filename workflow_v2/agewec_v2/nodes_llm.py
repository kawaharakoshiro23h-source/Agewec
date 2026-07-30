"""LLM-connected role nodes.

When llm.enabled is false these nodes delegate to the deterministic scaffolding in
nodes.py. When enabled, role decisions use RoleRunner while media/file operations
remain deterministic tools.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
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


def _review_context(
    state: WorkflowState,
    phase: str,
) -> dict[str, Any]:
    return dict(state.get("review_context", {}).get(phase, {}))


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

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        requested_duration = float(
            project.get("target_duration_seconds", 30)
        )
        if abs(float(data["target_duration_seconds"]) - requested_duration) > 0.01:
            raise ValueError(
                "Executive Producer must preserve "
                f"target_duration_seconds={requested_duration}"
            )
        requested_award = str(project.get("target_award", ""))
        if requested_award and data["target_award"] != requested_award:
            raise ValueError(
                f"Executive Producer must preserve target_award={requested_award}"
            )
        return {
            **data,
            "target_duration_seconds": requested_duration,
            "source_project": project,
        }

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
        transform=transform,
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
    def transform(data: dict[str, Any]) -> dict[str, Any]:
        inherited = list(brief.get("success_criteria", []))
        if not set(inherited).issubset(set(data["success_criteria"])):
            raise ValueError(
                "CreativeConcept must inherit ProjectBrief success criteria"
            )
        return data

    return _run_role(
        state,
        phase="creative_director",
        upstream={
            "project": state.get("project", {}),
            "project_brief": brief,
        },
        summary=lambda data: f"コンセプト「{data['title']}」をLLMが策定",
        fallback=deterministic.creative_director,
        transform=transform,
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
        if abs(float(data["total_seconds"]) - target) > 0.25:
            raise ValueError(
                f"storyboard duration {data['total_seconds']} "
                f"does not match target {target}"
            )
        actual = sum(float(cut["seconds"]) for cut in data["cuts"])
        if abs(actual - target) > 0.25:
            raise ValueError(
                f"cut duration total {actual} does not match target {target}"
            )
        narration_rate = float(
            state.get("config", {})
            .get("storyboard", {})
            .get("max_narration_characters_per_second", 8)
        )
        narration_issues = []
        for cut in data["cuts"]:
            compact = "".join(str(cut["narration"]).split())
            allowance = max(1, int(float(cut["seconds"]) * narration_rate))
            if len(compact) > allowance:
                narration_issues.append(
                    f"cut {cut['id']}: narration {len(compact)} chars "
                    f"exceeds {allowance}"
                )
        if narration_issues:
            raise ValueError("; ".join(narration_issues))
        return {
            **data,
            "total_seconds": target,
            "duration_source": "project.target_duration_seconds",
        }

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
    target_genre = deterministic.AWARD_GENRES.get(award)
    catalog = deterministic._load_catalog()
    photos = catalog.get("photos", [])
    candidates = []
    for index, photo in enumerate(photos, start=1):
        title = str(photo.get("title", ""))
        genres = list(photo.get("genres", []))
        local_path = deterministic._local_asset_path(
            photo.get("image_url", "")
        )
        file_size = None
        sha256 = None
        acquired_at = None
        if local_path:
            path = Path(local_path)
            stat = path.stat()
            file_size = stat.st_size
            acquired_at = datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        night_terms = ("夜", "ライトアップ", "イルミネーション")
        time_of_day = (
            "night"
            if any(term in title for term in night_terms)
            or "イルミネーション・夜景" in genres
            else "day_or_unspecified"
        )
        candidates.append(
            {
                "asset_id": f"asset-{index:03d}",
                "title": title,
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": genres,
                "areas": photo.get("areas", []),
                "local_path": local_path,
                "local_available": bool(local_path),
                "time_of_day": time_of_day,
                "visual_roles": genres,
                "target_award_match": (
                    target_genre is None or target_genre in genres
                ),
                "usage_scope": "agewec_submission",
                "rights_status": "approved_for_agewec_submission",
                "file_size_bytes": file_size,
                "sha256": sha256,
                "acquired_at": acquired_at,
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            not item["local_available"],
            not item["target_award_match"],
            item["asset_id"],
        ),
    )


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
    context = _review_context(state, "asset_curator")
    target_cut_id = context.get("target_cut_id")
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        candidate_map = {item["asset_id"]: item for item in candidates}
        valid_cut_ids = {int(cut["id"]) for cut in storyboard["cuts"]}
        if target_cut_id is not None and target_cut_id not in valid_cut_ids:
            raise ValueError(f"Unknown target_cut_id: {target_cut_id}")
        new_assignments: dict[int, dict[str, Any]] = {}
        invalid = []
        for selection in data["selections"]:
            cut_id = int(selection["cut_id"])
            if cut_id not in valid_cut_ids:
                invalid.append(f"unknown cut={cut_id}")
                continue
            if target_cut_id is not None and cut_id != target_cut_id:
                continue
            primary = selection["primary"]
            primary_id = primary["asset_id"]
            if primary_id not in candidate_map:
                invalid.append(f"cut={cut_id}, asset={primary_id}")
                continue
            alternatives = []
            seen = {primary_id}
            for alternative in selection.get("alternatives", []):
                asset_id = alternative["asset_id"]
                if asset_id not in candidate_map:
                    invalid.append(f"cut={cut_id}, asset={asset_id}")
                    continue
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                alternatives.append(
                    {
                        **candidate_map[asset_id],
                        "selection_reason": alternative["reason"],
                    }
                )
            new_assignments[cut_id] = {
                "cut_id": cut_id,
                "primary": {
                    **candidate_map[primary_id],
                    "selection_reason": primary["reason"],
                },
                "alternatives": alternatives,
            }
        if invalid:
            raise ValueError(
                "LLM selected unknown candidate/cut IDs: " + ", ".join(invalid)
            )
        existing = (
            _result_data(state, "asset_curator").get(
                "asset_assignments",
                [],
            )
            if target_cut_id is not None
            else []
        )
        merged = {
            int(item["cut_id"]): item
            for item in existing
            if int(item["cut_id"]) in valid_cut_ids
        }
        merged.update(new_assignments)
        missing_cuts = sorted(valid_cut_ids - set(merged))
        if target_cut_id is not None and target_cut_id not in new_assignments:
            raise ValueError(
                f"Targeted retry must select an asset for cut {target_cut_id}"
            )
        if missing_cuts:
            raise ValueError(
                "Every cut requires a primary asset; missing cuts: "
                + ", ".join(map(str, missing_cuts))
            )
        assignments = [merged[cut_id] for cut_id in sorted(merged)]
        selected_assets = [
            {
                **assignment["primary"],
                "cut_id": assignment["cut_id"],
            }
            for assignment in assignments
        ]
        return {
            "catalog_source": deterministic._load_catalog().get("source"),
            "available_candidate_count": len(candidates),
            "asset_assignments": assignments,
            "selected_assets": selected_assets,
            "missing_requirements": data.get("missing_requirements", []),
            "unassigned_cut_ids": [],
            "rights_check_required": False,
            "usage_scope": "agewec_submission",
            "targeted_revision_cut_id": target_cut_id,
        }

    return _run_role(
        state,
        phase="asset_curator",
        upstream={
            "project": state.get("project", {}),
            "storyboard": storyboard,
            "available_asset_candidates": candidates,
            "selection_rule": (
                "Assign exactly one primary asset to every requested cut. "
                "Use only supplied asset_id values. Prefer local_available assets."
            ),
            "target_cut_id": target_cut_id,
            "existing_asset_manifest": (
                _result_data(state, "asset_curator")
                if target_cut_id is not None
                else {}
            ),
        },
        summary=lambda data: (
            f"LLMが{len(data['asset_assignments'])}カットへ公式素材を割当"
        ),
        fallback=deterministic.asset_curator,
        transform=transform,
        warnings=(
            ["素材カタログの一部はローカル未取得"]
            if any(
                not item.get("local_available")
                for item in candidates
            )
            else []
        ),
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
    context = _review_context(state, "director")
    target_cut_id = context.get("target_cut_id")
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)

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
            asset_id = direction["asset_id"]
            if cut_id not in cut_map:
                invalid.append(f"unknown cut={cut_id}")
                continue
            if target_cut_id is not None and cut_id != target_cut_id:
                continue
            assignment = assignment_map[cut_id]
            choices = [assignment["primary"], *assignment["alternatives"]]
            asset_map = {item["asset_id"]: item for item in choices}
            if asset_id not in asset_map:
                invalid.append(
                    f"cut={cut_id}, asset={asset_id} is not assigned to cut"
                )
                continue
            if (
                direction.get("deviation_reason")
                and not direction["deviation_reason"].strip()
            ):
                direction["deviation_reason"] = None
            new_shots[cut_id] = {
                **cut_map[cut_id],
                "asset": asset_map[asset_id],
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
            "project": state.get("project", {}),
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
        },
        summary=lambda data: (
            f"LLMが{len(data['shots'])}カットの演出指示を確定"
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
