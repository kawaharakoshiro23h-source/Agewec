"""Phase 07A per-cut technical QA and review routing."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..fallbacks import common as deterministic
from .. import review_page
from ..media_tools import (
    MediaToolError,
    decode_check,
    extract_representative_frames,
    probe_media,
)
from ..phase_contracts import preserves_existing_artifact
from ..state import WorkflowState
from ..state_safe import SafeWorkflowState

from .common import (
    _attempt_json_path, _json_write, _ratio_dimensions,
)

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
    production = (
        state.get("cut_results", {})
        .get(str(current), {})
        .get("production", {})
    )
    generation_error = production.get("error") or {}
    attempt = int(
        (artifact or {}).get("attempt")
        or request.get("attempt")
        or state.get("cut_attempts", {}).get(str(current), 0)
    )
    issues: list[dict[str, Any]] = []
    technical: dict[str, Any] = {}
    frames: list[str] = []
    issue_class = "pass"
    try:
        if not artifact or not artifact.get("path"):
            if generation_error:
                error_type = str(
                    generation_error.get("exception_type") or "GenerationError"
                )
                error_message = str(
                    generation_error.get("message") or "詳細なし"
                )
                raise MediaToolError(
                    f"動画生成に失敗: {error_type}: {error_message}"
                )
            raise MediaToolError("生成動画のArtifactがない")
        technical = probe_media(artifact["path"])
        decode_check(artifact["path"])
        generation = (artifact or {}).get("generation") or {}
        generation_settings = generation.get("settings") or {}
        backend = str((artifact or {}).get("backend") or request.get("backend"))
        if backend == "runway":
            # Runway may normalize an allowed duration/ratio. QA must validate
            # against the values actually sent and billed, not a Comfy profile.
            expected = float(
                generation.get("billed_seconds")
                or generation.get("actual_seconds")
                or request.get("effective_seconds")
                or request.get("actual_seconds")
                or 0
            )
            expected_ratio = str(
                generation.get("ratio")
                or generation_settings.get("ratio")
                or request.get("ratio")
                or ""
            )
            expected_resolution = str(
                generation.get("resolution")
                or generation_settings.get("resolution")
                or request.get("resolution")
                or ""
            )
            if expected_ratio:
                expected_width, expected_height = _ratio_dimensions(
                    expected_ratio
                )
            elif expected_resolution:
                # "2K"は入力画像の縦横比で実寸が変わる。生成物が正常に
                # decodeできることは上で確認済みなので、架空の寸法との
                # 完全一致では判定しない。実寸はtechnicalへ記録される。
                expected_width = expected_height = 0
            else:
                raise ValueError(
                    "Runway生成結果にratioまたはresolutionがありません"
                )
        else:
            expected = float(request.get("actual_seconds") or 0)
            expected_width = int(request.get("width", 0))
            expected_height = int(request.get("height", 0))
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
        if expected_width and expected_height and (
            technical["width"] != expected_width
            or technical["height"] != expected_height
        ):
            issue_class = "generation_parameters"
            issues.append(
                {
                    "code": "RESOLUTION_MISMATCH",
                    "severity": "high",
                    "description": (
                        f"expected={expected_width}x"
                        f"{expected_height}, actual="
                        f"{technical['width']}x{technical['height']}"
                    ),
                    "evidence": [artifact["path"]],
                }
            )
        frames = extract_representative_frames(
            artifact["path"],
            deterministic._cut_path(state, current, "qa_frames"),
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
        "attempt": attempt,
        "seed": request.get("seed"),
        "artifact_path": (artifact or {}).get("path"),
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
        # 元画像が無いのが正常なのか異常なのかを、QA結果だけで判断できるようにする
        "generation_mode": request.get("generation_mode") or "image_to_video",
        "generation_error": generation_error or None,
        "generation_error_path": production.get("error_path"),
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
    # QA結果をカットのフォルダにも残す（run単位で追跡できるように）
    try:
        _json_write(deterministic._cut_path(state, current, "qa.json"), qa)
        if attempt > 0:
            _json_write(
                _attempt_json_path(state, current, attempt, "qa"),
                qa,
            )
    except OSError:
        pass
    # 人間が実物を見て判断できるよう、この時点でレビュー画面を更新する。
    # 生成に失敗しても本体を止めない（あくまで補助的な可視化）。
    try:
        merged_state = {
            **state,
            **update,
            "artifacts": list(state.get("artifacts", [])) + artifacts,
        }
        page = review_page.build_review_page(
            merged_state,
            deterministic._work_path(state, "review.html"),
        )
        update["cut_review_page"] = str(page)
    except Exception as exc:  # noqa: BLE001 - 可視化の失敗は致命的ではない
        update.setdefault("phase_results", {})
        update["cut_review_page"] = None
        print(f"[warn] レビュー画面の生成に失敗: {type(exc).__name__}: {exc}")
    return update


def commit_cut_qa(state: SafeWorkflowState) -> dict[str, Any]:
    current = int(state.get("current_cut_id") or 0)
    qa = state.get("cut_qa_results", {}).get(str(current), {})
    # 人間がレビューで下した判断は、AIのQA判定より優先する。
    # cut_id をキーにして保持し、他カットへ指示が漏れないようにする。
    human_decisions = dict(state.get("human_cut_qa_decisions", {}))
    human = human_decisions.get(str(current))
    human_applied = None
    if human:
        qa = {
            **qa,
            "verdict": human.get("verdict", "revise"),
            "recommended_route": human.get(
                "route", qa.get("recommended_route", "human_review")
            ),
            "recommended_feedback": human.get(
                "feedback", qa.get("recommended_feedback", "")
            ),
            "issue_class": human.get(
                "issue_class", qa.get("issue_class", "human_review")
            ),
            "decided_by": "human",
        }
        human_applied = {**human, "cut_id": current}
        # 使い終わった判断は破棄（次回の同カット再QAに残留させない）
        human_decisions.pop(str(current), None)
    approved = {int(value) for value in state.get("approved_cut_ids", [])}
    failed = {int(value) for value in state.get("failed_cut_ids", [])}
    queue = [
        int(value) for value in state.get("production_queue", [])
    ]
    artifacts = dict(state.get("production_artifacts", {}))
    context = dict(state.get("review_context", {}))
    feedback = dict(state.get("feedback", {}))
    route = qa.get("recommended_route", "human_review")
    attempt = int(
        qa.get("attempt")
        or (artifacts.get(str(current)) or {}).get("attempt")
        or state.get("production_requests", {})
        .get(str(current), {})
        .get("attempt", 0)
        or state.get("cut_attempts", {}).get(str(current), 0)
    )
    seed = (
        state.get("production_requests", {})
        .get(str(current), {})
        .get("seed")
    )

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
                "feedback_origin": (
                    "human" if qa.get("decided_by") == "human" else "ai_qa"
                ),
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
        if not preserves_existing_artifact(route):
            artifacts.pop(str(current), None)

    events = list(state.get("events", []))
    events.append(
        {
            "t": round(time.time(), 3),
            "type": "cut_qa_committed",
            "cut_id": current,
            "verdict": qa.get("verdict"),
            "route": route,
            "decided_by": "human" if human_applied else "ai",
        }
    )
    if human_applied:
        events.append(
            {
                "t": round(time.time(), 3),
                "type": "human_cut_decision_applied",
                "cut_id": current,
                "route": human_applied.get("route"),
                "feedback": human_applied.get("feedback", ""),
            }
        )
    # このカットに対する判断（誰が・何を選び・どこへ戻したか）を残す
    try:
        decision_record = {
            "cut_id": current,
            "attempt": attempt,
            "seed": seed,
            "verdict": qa.get("verdict"),
            "route": route,
            "decided_by": "human" if human_applied else "ai",
            "feedback": qa.get("recommended_feedback", ""),
            "issue_class": qa.get("issue_class", ""),
            "override_reason": (human_applied or {}).get("override_reason", ""),
            "original_verdict": (human_applied or {}).get("original_verdict"),
            "original_issues": (human_applied or {}).get("original_issues", []),
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        _json_write(
            deterministic._cut_path(state, current, "decision.json"),
            decision_record,
        )
        if attempt > 0:
            _json_write(
                _attempt_json_path(state, current, attempt, "decision"),
                decision_record,
            )
    except OSError:
        pass
    return {
        "approved_cut_ids": sorted(approved),
        "failed_cut_ids": sorted(failed),
        "production_queue": queue,
        "current_cut_id": next_cut,
        "production_artifacts": artifacts,
        "review_context": context,
        "feedback": feedback,
        "cut_qa_route": route,
        "human_cut_qa_decisions": human_decisions,
        "events": events,
    }
