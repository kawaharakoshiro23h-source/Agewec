"""Runtime nodes for Phase 05.5 through Phase 10.

【本番経路: 現役】実処理の中核（Phase 05.5〜10）。

    呼ばれる側: nodes_runtime
    担当      : Support Video Creator / 動画生成(ComfyUI) / Cut QA(ffprobe)
                / Post Production(FFmpeg結合) / Review Board / Provenance

秒数→フレーム数変換（8n+1制約）、ComfyUI呼び出し、FFmpegによる正規化・結合、
提出パッケージ生成をここで行う。Post Production は `ffmpeg_executed` を返す（実行済み）。
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import nodes as deterministic
from . import nodes_llm as llm_nodes
from . import review_page
from . import timing
from .backends import (
    BudgetStatus,
    Capabilities,
    ComfyClient,
    ComfyGenerationRequest,
    UnsupportedDurationError,
    VideoCostGuard,
    estimate_run_cost,
    resolve_backend,
    to_video_request,
)
from .media_tools import (
    MediaToolError,
    concat_video_clips,
    decode_check,
    downscale_image,
    extract_representative_frames,
    generate_mock_video,
    image_to_video_clip,
    normalize_video_clip,
    probe_media,
)
from .state import WorkflowState
from .state_safe import SafeWorkflowState


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


def _attempt_json_path(
    state: WorkflowState,
    cut_id: int,
    attempt: int,
    kind: str,
) -> Path:
    """カット内のattempt別メタデータJSONの保存先を返す。"""
    return deterministic._cut_path(
        state,
        cut_id,
        f"attempt_{int(attempt):02d}_{kind}.json",
    )


def _stable_seed(run_id: str, cut_id: int, attempt: int = 0) -> int:
    """run_id・cut_id・試行回数から決定論的なseedを作る。

    attempt を含めることで、同じ条件での「再生成」が別の結果になる。
    attempt が同じなら常に同じseed＝再現性は保たれる。
    """
    digest = hashlib.sha256(f"{run_id}:{cut_id}:{attempt}".encode()).digest()
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


def _ratio_dimensions(ratio: str) -> tuple[int, int]:
    """Convert a Runway ratio such as ``1280:720`` to dimensions."""
    try:
        width_text, height_text = ratio.split(":", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"不正なRunway ratio: {ratio!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"不正なRunway ratio: {ratio!r}")
    return width, height


def _runway_request_parameters(
    config: dict[str, Any],
    production: dict[str, Any],
    requested_seconds: float,
) -> dict[str, Any]:
    """Resolve the exact model-native values that Runway will receive.

    This is intentionally independent from the Comfy/LTX profile.  The same
    resolved duration and ratio are used by cost approval, API generation and
    QA, preventing estimates and technical checks from describing a different
    request than the one actually billed.
    """
    runway = dict(config.get("runway", {}))
    model = str(production.get("model") or "")
    models = dict(runway.get("models", {}))
    spec = dict(models.get(model, {}))
    if not model or not spec:
        raise ValueError(
            f"Runwayモデル {model or '(未設定)'} が config.runway.models にありません"
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
    ratio = str(spec.get("ratio") or runway.get("ratio") or "")
    if caps.resolutions and ratio not in caps.resolutions:
        raise ValueError(
            f"Runway {model}: ratio={ratio} は許容解像度 {caps.resolutions} にありません"
        )
    width, height = _ratio_dimensions(ratio)
    return {
        "model": model,
        "requested_seconds": requested_seconds,
        "actual_seconds": effective_seconds,
        "effective_seconds": effective_seconds,
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
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)

    existing = dict(state.get("production_requests", {}))
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
        image_path = str(shot.get("asset", {}).get("local_path") or "")
        if backend != "mock" and (
            not image_path or not Path(image_path).exists()
        ):
            blocking.append(
                f"cut {cut_id}: ローカル入力画像が存在しない: "
                f"{image_path or '(empty)'}"
            )
        common_request = {
            "cut_id": cut_id,
            "backend": backend,
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
                    config, production, seconds
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
        output_dir=deterministic._cut_path(state, int(request["cut_id"])),
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
            file_prefix=(
                f"attempt_{int(request.get('attempt', 1)):02d}"
            ),
        )
    )


def _video_backend(state: WorkflowState, backend: str):
    """設定名から動画バックエンドAdapterを返す。

    state・出力先などバックエンド固有の依存はここで束縛するため、
    呼び出し側は `adapter.generate(VideoRequest)` だけを知っていればよい。
    """
    config = state.get("config", {})
    return resolve_backend(
        backend,
        state=state,
        generate_comfy=_generate_comfy,
        generate_mock_video=generate_mock_video,
        output_path_for=lambda cut_id, attempt: (
            deterministic._cut_path(state, cut_id)
            / f"attempt_{attempt:02d}.mp4"
        ),
        runway_config=config.get("runway", {}),
        model=str(config.get("production", {}).get("model") or "") or None,
    )


def _run_cost_estimate(
    state: WorkflowState, backend: str, requests: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """H2で提示する、実行全体の概算費用。無料バックエンドでは None。"""
    try:
        adapter = _video_backend(state, backend)
        caps = adapter.capabilities(
            str((requests[0] if requests else {}).get("model") or "") or None
        )
    except Exception:  # noqa: BLE001
        return None
    if caps.cost_per_second_usd <= 0:
        return None
    cuts = [
        {
            "id": request.get("cut_id"),
            "seconds": float(
                request.get("requested_seconds")
                or request.get("actual_seconds", 0)
            ),
        }
        for request in requests
    ]
    try:
        return estimate_run_cost(caps, cuts)
    except UnsupportedDurationError as exc:
        return {"model": caps.model, "total_usd": 0.0, "error": str(exc)}


def _video_budget(state: WorkflowState, backend: str, request: dict[str, Any]):
    """課金前ガード。(guard, status) を返す。無料バックエンドは (None, None)。

    「実課金累計 + 次回見積 <= 承認上限」を送信前に検査するための材料を作る。
    """
    config = state.get("config", {}).get("production", {}).get("cost_guard", {})
    if not config.get("enabled", False):
        return None, None
    # 無料であることが確実なバックエンドだけ素通しする（fail-closed）。
    # 見積が取れない場合は「安全側 = 送信させない」に倒す。
    free_backends = set(
        config.get("free_backends", ["mock", "comfy"])
    )
    guard = VideoCostGuard(
        ledger_path=deterministic._work_path(state, "video_cost_ledger.json"),
        limit_usd=float(config.get("limit_usd", 20.0)),
    )
    try:
        adapter = _video_backend(state, backend)
        model = str(request.get("model") or "") or None
        caps = adapter.capabilities(model)
    except Exception as exc:  # noqa: BLE001
        if backend in free_backends:
            return None, None
        # 課金系で見積不能 → 送信を止める
        return guard, BudgetStatus(
            limit_usd=guard.limit_usd,
            spent_usd=guard.spent_usd,
            next_estimate_usd=float("inf"),
            generations=0,
        )
    if caps.cost_per_second_usd <= 0 and backend in free_backends:
        return None, None       # ローカル実行は課金なし
    try:
        estimate = caps.estimate_cost(
            float(
                request.get("requested_seconds")
                or request.get("actual_seconds", 0)
            )
        )
    except UnsupportedDurationError:
        # 尺がモデルの上限を超える → 短く切り詰めず、ここで止める
        return guard, BudgetStatus(
            limit_usd=guard.limit_usd,
            spent_usd=guard.spent_usd,
            next_estimate_usd=float("inf"),
            generations=0,
        )
    return guard, guard.check(estimate)


def _technical_request_signature(request: dict[str, Any]) -> tuple[Any, ...]:
    """Fields whose change can resolve duration/resolution QA failures."""
    return (
        request.get("backend"),
        request.get("model"),
        request.get("requested_seconds"),
        request.get("actual_seconds"),
        request.get("ratio"),
        request.get("width"),
        request.get("height"),
        request.get("frames"),
        request.get("fps"),
    )


def _unchanged_technical_retry(
    state: WorkflowState,
    cut_id: int,
    request: dict[str, Any],
) -> bool:
    """Detect a paid retry that cannot fix the immediately preceding QA issue."""
    previous = state.get("cut_results", {}).get(str(cut_id), {})
    qa = previous.get("qa") or {}
    issue_codes = {
        item.get("code")
        for item in qa.get("issues", [])
        if isinstance(item, dict)
    }
    if not issue_codes.intersection({"DURATION_MISMATCH", "RESOLUTION_MISMATCH"}):
        return False
    previous_request = (previous.get("production") or {}).get("request") or {}
    if not previous_request:
        return False
    return _technical_request_signature(
        previous_request
    ) == _technical_request_signature(request)


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

    if str(request.get("backend")) == "runway":
        if _unchanged_technical_retry(state, current, request):
            update = deterministic._complete(
                state,
                phase,
                summary=f"Cut {current}の同一条件での再課金を停止",
                data={
                    "cut_id": current,
                    "issue_class": "generation_parameters",
                    "api_called": False,
                    "reason": "technical_generation_settings_unchanged",
                },
                status="error",
                confidence=1.0,
                blocking_issues=[
                    "前回の尺・解像度エラーに対して生成条件が変わっていません。"
                    "同じ有料API呼び出しを停止しました。"
                ],
            )
            update.update({"current_cut_id": current})
            return update

    attempts = dict(state.get("cut_attempts", {}))
    attempt = int(attempts.get(str(current), 0)) + 1
    attempts[str(current)] = attempt

    # 「同じ条件で再生成」は support_video_creator を通らず直接ここへ戻るため、
    # 生成直前に今回の attempt から seed を引き直す。
    # これがないと再生成しても同じseed＝同じ映像になる。
    request = {
        **request,
        "seed": _stable_seed(
            str(state.get("run_id", "")), current, attempt - 1
        ),
        "attempt": attempt,
    }
    requests[str(current)] = request

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
    output_dir = deterministic._cut_path(state, current)
    output_dir.mkdir(parents=True, exist_ok=True)
    # このカットの入力を run 内に残す（何を使って生成したかを追跡できるように）
    _json_write(output_dir / "request.json", request)
    _json_write(
        _attempt_json_path(state, current, attempt, "request"),
        request,
    )
    source_image = Path(str(request.get("image_path") or ""))
    if source_image.exists():
        destination = output_dir / f"source{source_image.suffix}"
        if not destination.exists():
            try:
                shutil.copy2(source_image, destination)
            except OSError:
                pass
    backend = str(request["backend"])
    issues: list[str] = []
    generation: dict[str, Any] = {}
    output_path = ""
    guard, budget = _video_budget(state, backend, request)
    if budget is not None and not budget.allowed:
        # 課金上限を超えるため、APIへ送信せずに停止する。
        # 「実課金累計 + 次回見積 <= 上限」を満たさない再生成は行わない。
        update = deterministic._complete(
            state,
            phase,
            summary="動画生成の予算上限に到達",
            data={
                "cut_id": current,
                "issue_class": "budget",
                "attempt": attempt,
                "budget": {
                    "limit_usd": budget.limit_usd,
                    "spent_usd": budget.spent_usd,
                    "next_estimate_usd": budget.next_estimate_usd,
                    "projected_usd": budget.projected_usd,
                },
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                f"予算上限: 実課金 ${budget.spent_usd:.2f} + 見積 "
                f"${budget.next_estimate_usd:.2f} > 上限 "
                f"${budget.limit_usd:.2f}"
            ],
        )
        update.update({"current_cut_id": current, "cut_attempts": attempts})
        return update
    try:
        # 生成は必ず Adapter 経由で行う。バックエンドごとの分岐はここに無い
        # （Runway等を足しても、この呼び出しは変わらない）。
        adapter = _video_backend(state, backend)
        result = adapter.generate(to_video_request(request, attempt=attempt))
        output_path = result.output_path
        generation = {
            **result.settings,
            **result.to_record(),
            "output_path": output_path,
        }
    except Exception as exc:
        issues.append(f"{type(exc).__name__}: {exc}")

    elapsed = round(time.monotonic() - started, 3)
    # 実課金を台帳へ積む（見積ではなく実額。次回の上限判定に使う）
    if guard is not None and not issues:
        try:
            guard.record(
                cut_id=current,
                provider=backend,
                model=str(request.get("model") or ""),
                cost_usd=float(generation.get("cost_usd", 0.0)),
                billed_seconds=float(
                    generation.get("billed_seconds")
                    or request.get("actual_seconds", 0)
                ),
                # VideoResult.to_record() は job_id を出す（prompt_id ではない）
                job_id=str(generation.get("job_id") or "") or None,
                estimated_usd=(
                    budget.next_estimate_usd if budget else None
                ),
            )
        except OSError:
            pass
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
            "production_requests": requests,
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
            expected_width, expected_height = _ratio_dimensions(expected_ratio)
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
        if (
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


_PHASE_PRESENTATION: tuple[dict[str, Any], ...] = (
    {
        "id": "executive_producer",
        "number": "01",
        "title": "Executive Producer（統括プロデューサー：制作要件の定義）",
        "kind": "AI（LLM）",
        "purpose": "制作依頼を、全工程が共有する目的・制約・成功基準へ変換する。",
        "input_source": "Project設定（人間が最初に指定）",
        "inputs": [
            "theme: 制作テーマ",
            "target_award: 狙う部門・評価軸",
            "target_duration_seconds: 最終目標尺",
        ],
        "process": "対象視聴者、納品物、制約、成功基準を定義する。",
        "output": "ProjectBrief JSON",
        "next": "Creative Director（コンセプト設計）",
    },
    {
        "id": "creative_director",
        "number": "02",
        "title": "Creative Director（クリエイティブディレクター：コンセプト設計）",
        "kind": "AI（LLM）",
        "purpose": "企画全体のコンセプト、色、トーン、カメラ意図を統一する。",
        "input_source": "Project設定 + ProjectBrief",
        "inputs": [
            "objective / audience / constraints",
            "success_criteria",
            "target_award / target_duration_seconds",
        ],
        "process": "作品タイトル、訴求、視覚言語、音響方針、全体演出意図を策定する。",
        "output": "CreativeConcept JSON",
        "next": "Writer / Storyboard（台本・絵コンテ）",
    },
    {
        "id": "writer_storyboard",
        "number": "03",
        "title": "Writer / Storyboard（脚本・絵コンテ：台本とカット構成）",
        "kind": "AI＋コード",
        "purpose": "コンセプトを、指定尺に収まる具体的なカット列へ分解する。",
        "input_source": "ProjectBrief + CreativeConcept",
        "inputs": [
            "作品コンセプトと成功基準",
            "目標尺",
            "カメラ意図・トーン",
        ],
        "process": "各カットの場面、秒数、ナレーション、時間帯、場所、被写体を構成し、コードで尺を補正する。",
        "output": "Storyboard JSON（cuts[]）",
        "next": "Asset Curator（素材選定）",
    },
    {
        "id": "asset_curator",
        "number": "04",
        "title": "Asset Curator（素材キュレーター：公式素材の選定）",
        "kind": "コード＋AI",
        "purpose": "各カットに、実在するAGEWEC公式写真を最低1枚割り当てる。",
        "input_source": "Storyboard + ローカル素材カタログ",
        "inputs": [
            "cut id / time_of_day / location / subject / visual_role",
            "素材ID、ジャンル、地域、時間帯、ローカルパス",
        ],
        "process": "コードが適合度を採点して写真を確定し、LLMは選定理由だけを説明する。",
        "output": "AssetManifest JSON（primary + alternatives）",
        "next": "Director（演出設計）",
    },
    {
        "id": "director",
        "number": "05",
        "title": "Director（監督：カット別の演出とプロンプト設計）",
        "kind": "AI（LLM）",
        "purpose": "各カットと選定写真を、動画生成に必要な個別演出へ変換する。",
        "input_source": "CreativeConcept + Storyboard + AssetManifest",
        "inputs": [
            "カット内容と秒数",
            "選定済みasset_id",
            "全体のカメラ意図・連続性ルール",
        ],
        "process": "positive/negative prompt、カメラ移動、動きの強度、演出根拠を作る。",
        "output": "DirectionPlan JSON（shots[]）",
        "next": "Support Video Creator（生成条件の変換）",
    },
    {
        "id": "support_video_creator",
        "number": "05.5",
        "title": "Support Video Creator（生成条件の変換：秒数→技術パラメータ）",
        "kind": "コード（自動）",
        "purpose": "演出指示を、ComfyUIが実行できる技術パラメータへ安全に変換する。",
        "input_source": "DirectionPlan + Production設定",
        "inputs": [
            "画像パスと生成Prompt",
            "Storyboard秒数",
            "解像度、FPS、steps、モデル制約",
        ],
        "process": "秒数をLTX互換フレーム数へ変換し、seedや出力設定を確定する。",
        "output": "ProductionRequest JSON（カット別）",
        "next": "Image / Video Production（映像生成）",
    },
    {
        "id": "image_video_production",
        "number": "06",
        "title": "Image / Video Production（映像生成：実写を起点にAIで動画化）",
        "kind": "生成AI（動画）",
        "purpose": "確定済み画像とPromptから、カット単位の実MP4を生成する。",
        "input_source": "ProductionRequest",
        "inputs": [
            "入力画像",
            "positive/negative prompt",
            "frames / fps / width / height / steps / seed",
        ],
        "process": "ComfyUI APIへ投入し、完了を待って生成動画と実行情報を保存する。",
        "output": "MediaArtifact（MP4 + generation metadata）",
        "next": "Cut Visual QA（カット品質検査）",
    },
    {
        "id": "cut_visual_qa",
        "number": "07A",
        "title": "Cut Visual QA（カット品質検査：尺・破綻の確認と差し戻し）",
        "kind": "コード＋人間確認",
        "purpose": "生成直後の各カットを検査し、問題の種類に応じて必要な工程だけへ戻す。",
        "input_source": "ProductionRequest + 生成MP4",
        "inputs": [
            "要求尺・解像度・FPS",
            "生成動画",
            "代表フレーム",
        ],
        "process": "デコード、尺、解像度を検査し、passまたは修正先を判定する。",
        "output": "CutQAResult JSON",
        "next": "合格→次カット ／ 不合格→Director（演出修正）・Asset Curator（素材変更）・Support Video Creator（条件変更）",
    },
    {
        "id": "visual_qa",
        "number": "07B",
        "title": "Sequence Readiness QA（全体整合検査：編集へ進めるかの判定）",
        "kind": "コード／AI",
        "purpose": "全カットが揃い、最終編集へ進める状態かを確認する。",
        "input_source": "全CutQAResult + 全生成Artifact",
        "inputs": [
            "承認済みカットID",
            "失敗カットID",
            "技術QA結果",
        ],
        "process": "欠落、不整合、未承認カットを検査して次の経路を決定する。",
        "output": "VisualQAResult JSON",
        "next": "Post Production（編集・仕上げ）",
    },
    {
        "id": "post_production",
        "number": "08",
        "title": "Post Production（編集・仕上げ：結合と最終尺の調整）",
        "kind": "コード（FFmpeg）",
        "purpose": "承認済みカットを正規化・結合し、指定尺の最終MP4にする。",
        "input_source": "Storyboard + 承認済みMediaArtifact",
        "inputs": [
            "カット順・目標秒数",
            "各MP4",
            "最終解像度・FPS",
        ],
        "process": "各カットをトリム・正規化して結合し、最終動画を再検査する。",
        "output": "final_video.mp4 + EditManifest + TechnicalReport",
        "next": "Review Board（審査会）",
    },
    {
        "id": "review_board",
        "number": "09",
        "title": "Review Board（審査会：提出水準に達したかの総合評価）",
        "kind": "AI／人間",
        "purpose": "最終動画と制作要件を採点し、提出可否または修正を判断する。",
        "input_source": "最終MP4 + 技術QA + 上流成果物",
        "inputs": [
            "コンセプト・Storyboard・素材証跡",
            "最終Technical QA",
            "評価rubric",
        ],
        "process": "AI採点または人間確認を行い、pass/reviseを返す。",
        "output": "ReviewBoardResult JSON",
        "next": "Final Submission Review（最終提出承認）",
    },
    {
        "id": "final_submission",
        "number": "H3",
        "title": "Final Submission Review（最終提出承認：人間による可否判断）",
        "kind": "人間の承認",
        "purpose": "提出直前に最終動画と未解決事項を確認し、公開を承認する。",
        "input_source": "ReviewBoardResult + final_video.mp4",
        "inputs": [
            "最終動画",
            "最終技術QA",
            "警告・Review Board結果",
        ],
        "process": "approve / retry_with_feedback / abortを選択する。",
        "output": "ReviewDecision",
        "next": "Provenance & Submission Package（証跡・提出物）",
    },
    {
        "id": "provenance",
        "number": "10",
        "title": "Provenance & Submission Package（証跡・提出物：制作過程の記録と提出パッケージ）",
        "kind": "コード（自動）",
        "purpose": "動画と全判断記録を、第三者が追跡できる提出Packageへまとめる。",
        "input_source": "全phase_results + reviews + events + artifacts",
        "inputs": [
            "各工程の構造化出力",
            "人間・自動承認履歴",
            "動画・QA・編集成果物",
        ],
        "process": "証跡をサニタイズし、レポート、JSON、ハッシュManifestを生成する。",
        "output": "Submission Package（HTML / JSON / JSONL / MP4）",
        "next": "提出完了",
    },
)


def _compact_text(value: Any, limit: int = 420) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _phase_actual_items(
    phase: str,
    result: dict[str, Any],
    state: WorkflowState,
) -> list[tuple[str, Any]]:
    data = result.get("data", {})
    if phase == "executive_producer":
        return [
            ("目的", data.get("objective")),
            ("対象", data.get("audience")),
            ("狙う賞", data.get("target_award")),
            ("目標尺", f"{data.get('target_duration_seconds')}秒"),
            ("成功基準", data.get("success_criteria", [])),
        ]
    if phase == "creative_director":
        return [
            ("コンセプト", data.get("title")),
            ("一行企画", data.get("logline")),
            ("トーン", data.get("tone", [])),
            ("カメラ意図", data.get("camera_intent", {})),
        ]
    if phase == "writer_storyboard":
        cuts = [
            (
                f"Cut {cut.get('id')}: {cut.get('name')} / "
                f"{cut.get('seconds')}秒 / {cut.get('time_of_day')} / "
                f"{cut.get('location')} — {cut.get('scene')}"
            )
            for cut in data.get("cuts", [])
        ]
        return [
            ("合計尺", f"{data.get('total_seconds')}秒"),
            ("カット構成", cuts),
            ("尺補正", data.get("duration_adjustment", {})),
        ]
    if phase == "asset_curator":
        assignments = []
        for item in data.get("asset_assignments", []):
            primary = item.get("primary", {})
            assignments.append(
                (
                    f"Cut {item.get('cut_id')}: "
                    f"{primary.get('asset_id')} {primary.get('title')} / "
                    f"コード根拠: {primary.get('selection_reason', '')} / "
                    f"LLM説明: {primary.get('llm_rationale', '')}"
                )
            )
        return [
            ("選定方式", data.get("selection_mode")),
            ("確定素材", assignments),
        ]
    if phase == "director":
        shots = [
            (
                f"Cut {shot.get('id')}: asset="
                f"{shot.get('asset', {}).get('asset_id')} / "
                f"camera={shot.get('camera_motion')} / "
                f"prompt={_compact_text(shot.get('positive_prompt'), 260)} / "
                f"根拠={shot.get('rationale', '')}"
            )
            for shot in data.get("shots", [])
        ]
        return [
            ("カット別演出", shots),
            ("連続性確認", data.get("continuity_checks", [])),
        ]
    if phase == "support_video_creator":
        requests = state.get("production_requests", {})
        return [
            (
                "生成Request",
                [
                    f"Cut {item.get('cut_id')}: {_request_summary(item)}"
                    for item in requests.values()
                ],
            )
        ]
    if phase == "image_video_production":
        artifacts = state.get("production_artifacts", {})
        return [
            ("生成済みカット", sorted(map(int, artifacts.keys()))),
            (
                "生成動画",
                [
                    f"Cut {key}: {value.get('path')}"
                    for key, value in sorted(artifacts.items())
                ],
            ),
        ]
    if phase == "cut_visual_qa":
        qa_results = state.get("cut_qa_results", {})
        return [
            (
                "カット別QA",
                [
                    (
                        f"Cut {key}: {value.get('verdict')} / "
                        f"{value.get('issue_class')}"
                    )
                    for key, value in sorted(qa_results.items())
                ],
            ),
            ("承認済みカット", state.get("approved_cut_ids", [])),
        ]
    if phase == "visual_qa":
        return [
            ("判定", data.get("verdict") or result.get("status")),
            ("問題", data.get("issues", [])),
            ("次の経路", data.get("route") or data.get("recommended_route")),
        ]
    if phase == "post_production":
        technical = data.get("technical_qa", {})
        return [
            ("実装", data.get("implementation")),
            ("最終動画", _portable_path(data.get("output_path") or "—")),
            (
                "Technical QA",
                {
                    "status": technical.get("status"),
                    "duration_seconds": technical.get("duration_seconds"),
                    "resolution": (
                        f"{technical.get('width')}x{technical.get('height')}"
                    ),
                    "fps": technical.get("fps"),
                },
            ),
        ]
    if phase == "review_board":
        return [
            ("モード", data.get("mode", "ai")),
            ("判定", data.get("verdict")),
            ("平均点", data.get("average")),
            ("推奨事項", data.get("recommendations", [])),
        ]
    if phase == "provenance":
        return [
            ("提出Package", data.get("package_dir")),
            ("最終動画", data.get("final_video")),
            ("Process Report", data.get("process_report")),
            ("Manifest", data.get("manifest")),
        ]
    return [("生成結果", result.get("summary", ""))]


def _format_markdown_value(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(_compact_text(item) for item in value) or "—"
    if isinstance(value, dict):
        return "; ".join(
            f"{key}={_compact_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ) or "—"
    return _compact_text(value) or "—"


def _feedback_actual_items(result: dict[str, Any]) -> list[tuple[str, Any]]:
    feedback = str(result.get("feedback_received") or "")
    if not feedback:
        return []
    previous = result.get("previous_data")
    current = result.get("data", {})
    changed = []
    if isinstance(previous, dict) and isinstance(current, dict):
        changed = sorted(
            key
            for key in set(previous) | set(current)
            if previous.get(key) != current.get(key)
        )
    feedback_label = {
        "human": "人間フィードバック",
        "ai_qa": "QAによる自動修正提案",
        "system": "システム修正情報",
    }.get(str(result.get("feedback_origin")), "前工程からの修正情報")
    return [
        (feedback_label, feedback),
        ("フィードバック状態", result.get("feedback_status")),
        (
            "反映確認用差分",
            changed or result.get("feedback_application_evidence") or "変更なし",
        ),
    ]


def _render_html_value(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "<span class='muted'>—</span>"
        return "<ul>" + "".join(
            f"<li>{html.escape(_compact_text(item))}</li>"
            for item in value
        ) + "</ul>"
    if isinstance(value, dict):
        if not value:
            return "<span class='muted'>—</span>"
        return "<dl>" + "".join(
            f"<dt>{html.escape(str(key))}</dt>"
            f"<dd>{html.escape(_compact_text(item))}</dd>"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ) + "</dl>"
    return f"<span>{html.escape(_compact_text(value)) or '—'}</span>"


def _process_markdown(state: WorkflowState, video_name: str) -> str:
    lines = [
        "# AGEWEC Production Process Report",
        "",
        f"- Run ID: `{state.get('run_id')}`",
        f"- Final video: `{video_name}`",
        f"- Target duration: "
        f"{state.get('project', {}).get('target_duration_seconds')} seconds",
        "",
        "## 全体ワークフロー",
        "",
        " → ".join(
            f"{item['number']} {item['title']}"
            for item in _PHASE_PRESENTATION
        ),
        "",
        "各工程の後には設定されたReview Gateがあり、承認、対象工程の再実行、"
        "中止を選べます。Cut QAは問題種別に応じて生成、演出、素材選定へ戻ります。",
        "",
        "## ノードごとの入出力と実行結果",
        "",
    ]
    phase_results = state.get("phase_results", {})
    reviews = state.get("reviews", [])
    for static_guide in _PHASE_PRESENTATION:
        # 実際に使ったバックエンドに合わせて説明文を差し替える
        guide = _guide_for_backend(static_guide, state)
        phase = guide["id"]
        result = phase_results.get(phase, {})
        related_reviews = [
            review
            for review in reviews
            if review.get("phase") == phase
            or review.get("source_phase") == phase
        ]
        lines.extend(
            [
                f"### {guide['number']} {guide['title']}",
                "",
                f"- 種別: `{guide['kind']}`",
                f"- 目的: {guide['purpose']}",
                f"- 入力元: {guide['input_source']}",
                f"- 入力情報: {'; '.join(guide['inputs'])}",
                f"- 処理: {guide['process']}",
                f"- 出力形式: `{guide['output']}`",
                f"- 次工程: {guide['next']}",
                f"- 実行状態: `{result.get('status', 'review-only')}`",
                f"- 実行要約: {result.get('summary', 'Review Gateとして実行')}",
                "",
            ]
        )
        if result:
            actual_items = _phase_actual_items(phase, result, state)
            actual_items.extend(_feedback_actual_items(result))
            for label, value in actual_items:
                lines.append(
                    f"- 実際の{label}: {_format_markdown_value(value)}"
                )
        for review in related_reviews:
            lines.append(
                f"- 承認: `{review.get('action')}` "
                f"by `{review.get('decided_by')}`"
                + (
                    f" — {review.get('feedback')}"
                    if review.get("feedback")
                    else ""
                )
            )
        lines.append("")
    lines.extend(
        [
            "## 補足",
            "",
            "このレポートは公開可能な入力、構造化出力、判断理由、承認履歴を"
            "説明します。内部Chain-of-ThoughtやAPIサーバーログは掲載しません。",
            "",
        ]
    )
    return "\n".join(lines)


def _cut_media_paths(state: WorkflowState, cut_id: int) -> tuple[str | None, str | None]:
    """提出Package内での（元画像, 生成動画）の相対パス。

    実体は `_copy_cut_sources` が同じ命名で配置する。
    """
    shots = {
        int(s.get("id", s.get("cut_id", 0))): s
        for s in state.get("phase_results", {})
        .get("director", {})
        .get("data", {})
        .get("shots", [])
    }
    asset = (shots.get(cut_id, {}) or {}).get("asset", {}) or {}
    source = Path(str(asset.get("local_path") or ""))
    source_rel = (
        f"artifacts/sources/cut_{cut_id:02d}_source{source.suffix}"
        if source.name
        else None
    )
    clip = Path(
        str(
            (state.get("production_artifacts", {}).get(str(cut_id)) or {}).get(
                "path"
            )
            or ""
        )
    )
    clip_rel = (
        f"artifacts/cuts/cut_{cut_id:02d}{clip.suffix}" if clip.name else None
    )
    return source_rel, clip_rel


# ホームディレクトリの一般形（macOS: /Users/名前, Linux: /home/名前）。
# レポートを生成した機械と、パスが記録された機械が違う場合（過去runの再生成、
# CI、別環境での検証）でもユーザー名が残らないよう、実行環境の HOME だけに
# 頼らず正規表現でも畳む。
_HOME_PATTERN = re.compile(r"/(?:Users|home)/[^/\"',\s]+/")


def _llm_usage_totals(state: WorkflowState) -> dict[str, Any]:
    """LLM呼び出しのトークン数と費用を、この run の分だけ合計する。

    `work/llm_cost_ledger.json` は全run累積なので使えない。各フェーズの
    `phase_results[phase]["llm"]["usage"]` を足し、config の単価で費用を出す。
    古い run では usage が "***" にマスクされていることがあるため、数値化
    できたものだけを集計し、その旨を `available` で返す。
    """
    llm_config = state.get("config", {}).get("llm", {})
    guard = llm_config.get("cost_guard", {})
    in_rate = float(guard.get("input_cost_per_million_usd", 0.0))
    out_rate = float(guard.get("output_cost_per_million_usd", 0.0))

    prompt = completion = 0
    calls = 0
    masked = False
    for result in state.get("phase_results", {}).values():
        usage = ((result or {}).get("llm") or {}).get("usage") or {}
        if not usage:
            continue
        calls += 1
        for key, add in (("prompt_tokens", "p"), ("completion_tokens", "c")):
            raw = usage.get(key)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                masked = True
                continue
            if add == "p":
                prompt += value
            else:
                completion += value

    cost = (prompt * in_rate + completion * out_rate) / 1_000_000
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_usd": round(cost, 6),
        "available": bool(prompt or completion),
        "masked": masked,
        "model": llm_config.get("model") or guard.get("pricing_model", ""),
    }


def _video_cost_summary(state: WorkflowState) -> dict[str, Any]:
    """この run の動画生成の実課金を台帳から読む（推定ではなく実額）。"""
    path = deterministic._work_path(state, "video_cost_ledger.json")
    try:
        ledger = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"spent_usd": 0.0, "generations": []}
    return {
        "spent_usd": float(ledger.get("spent_usd", 0.0)),
        "generations": list(ledger.get("generations", [])),
    }


def _human_intervention_summary(state: WorkflowState) -> dict[str, int]:
    """人間が何回・どう介入したかを数える（自律性の説明に使う）。"""
    counts = {"approve": 0, "retry_with_feedback": 0, "override": 0}
    for review in state.get("reviews", []):
        if str(review.get("decided_by")) != "human":
            continue
        action = str(review.get("action"))
        if action in counts:
            counts[action] += 1
    for cut in state.get("cut_results", {}).values():
        if ((cut or {}).get("qa") or {}).get("decided_by") == "human":
            counts["override"] += 1
    return counts


def _portable_path(value: Any) -> str:
    """提出物に載せるパスから、実行環境固有の部分を取り除く。

    `/Users/<name>/.../Agewec/workflow_v2/work/...` のような絶対パスをそのまま
    載せると、提出先にユーザー名とディレクトリ構成が見える。まずリポジトリからの
    相対表記へ畳み、残った絶対パスはホーム部分を `~/` に置き換える。
    """
    text = str(value)
    if not text:
        return text
    for root in (deterministic.WORKFLOW_ROOT.parent, Path.home()):
        text = text.replace(str(root) + "/", "").replace(str(root), ".")
    return _HOME_PATTERN.sub("~/", text)


# バックエンドごとの説明文。_PHASE_PRESENTATION は静的な辞書なので、
# ComfyUI/LTX固定の文言を実際に使った経路へ差し替える。
# （Runwayで実行したのに「ComfyUIが実行できる技術パラメータへ変換」と
#   書かれていると、レポートが事実と食い違う）
_BACKEND_PRESENTATION: dict[str, dict[str, dict[str, Any]]] = {
    "runway": {
        "support_video_creator": {
            "purpose": (
                "演出指示を、動画生成APIが受け付ける技術パラメータへ"
                "安全に変換する。"
            ),
            "inputs": [
                "画像パスと生成Prompt",
                "Storyboard秒数",
                "モデルの許容尺・解像度・単価",
            ],
            "process": (
                "秒数をモデルの許容尺へ丸め、解像度・seed・概算費用を確定する。"
            ),
        },
        "image_video_production": {
            "inputs": [
                "入力画像",
                "positive/negative prompt",
                "model / duration / ratio / seed",
            ],
            "process": (
                "Runway APIへ投入し、完了を待って生成動画と実行情報・"
                "実課金額を保存する。"
            ),
        },
    },
}


def _guide_for_backend(
    guide: dict[str, Any],
    state: WorkflowState,
) -> dict[str, Any]:
    backend = str(
        state.get("config", {}).get("production", {}).get("backend", "")
    )
    override = _BACKEND_PRESENTATION.get(backend, {}).get(guide["id"])
    return {**guide, **override} if override else guide


def _request_summary(item: dict[str, Any]) -> str:
    """ProductionRequest 1件を、その契約に存在する項目だけで1行にする。

    Runway契約には frames / steps / fps が無く（尺と解像度はモデル側が決める）、
    Comfy/LTX契約にはモデル名や許容尺が無い。両方を同じ書式で出そうとすると
    「None frames / Nonefps / None steps」のように存在しない項目が None として
    表示されるため、契約ごとに項目を選ぶ。
    """
    parts: list[str] = []
    width, height = item.get("width"), item.get("height")
    if width and height:
        parts.append(f"{width}x{height}")

    if str(item.get("request_contract", "")) == "runway_model_native":
        if item.get("model"):
            parts.append(str(item["model"]))
        seconds = item.get("effective_seconds") or item.get("actual_seconds")
        if seconds:
            parts.append(f"{float(seconds):g}秒")
    else:
        for value, unit in (
            (item.get("frames"), " frames"),
            (item.get("fps"), "fps"),
            (item.get("steps"), " steps"),
        ):
            if value is not None:
                parts.append(f"{value}{unit}")

    if item.get("seed") is not None:
        parts.append(f"seed {item['seed']}")
    return ", ".join(parts) if parts else "—"


def _generation_conditions(
    request: dict[str, Any],
    qa: dict[str, Any],
) -> str:
    """解像度・fps・尺を「実際に出力された値」優先で1行にまとめる。

    fps はバックエンドによって Request に存在しない（Runwayはモデル側が決める
    ためリクエスト項目が無く、LTXのときだけ入る）。Requestだけを見ると空文字に
    なり「1280×720 / fps / ...」と壊れた表示になるため、QAがffprobeで実測した
    `technical` を第一の情報源とし、無い場合のみ Request で補う。
    項目が両方から得られないときは、その項目ごと省く。
    """
    technical = qa.get("technical") or {}
    parts: list[str] = []

    width = technical.get("width") or request.get("width")
    height = technical.get("height") or request.get("height")
    if width and height:
        parts.append(f"{width}×{height}")

    fps = technical.get("fps") or request.get("fps")
    if fps:
        value = float(fps)
        parts.append(f"{value:g}fps")

    seconds = (
        technical.get("duration_seconds")
        or request.get("actual_seconds")
        or request.get("requested_seconds")
    )
    if seconds:
        parts.append(f"{float(seconds):.2f}秒")

    return " / ".join(parts) if parts else "—"


# カード表示で同じ内容を示す項目。文字列の羅列を二重に出さないため、
# カードがあるフェーズではこれらの行を省く（絶対パスの露出もここで消える）。
_CARD_COVERED_ITEMS: dict[str, set[str]] = {
    "director": {"カット別演出"},
    "image_video_production": {"生成済みカット", "生成動画"},
}


def _phase_visual_cards(phase: str, state: WorkflowState) -> str:
    """演出設計・映像生成の「実物」を並べたカードを返す。

    テキストの羅列では「この写真にこの指示でこう動いた」が判断できないため、
    元画像・プロンプト・カメラワーク・生成映像を1カットずつ並べる。
    """
    if phase not in {"director", "image_video_production"}:
        return ""
    results = state.get("phase_results", {})
    shots = sorted(
        results.get("director", {}).get("data", {}).get("shots", []),
        key=lambda s: int(s.get("id", s.get("cut_id", 0))),
    )
    if not shots:
        return ""
    cuts = {
        int(c.get("id", 0)): c
        for c in results.get("writer_storyboard", {}).get("data", {}).get("cuts", [])
    }
    requests = state.get("production_requests", {})
    qa_results = state.get("cut_qa_results", {})
    attempts = state.get("cut_attempts", {})

    box = (
        "background:#fafbfc;border:1px solid #e2e6ec;border-radius:10px;"
        "padding:12px 14px;margin-bottom:10px;"
    )
    label = "font-size:11px;color:#5b6570;margin:8px 0 2px;"
    # レポート全体のCSSは pre{background:#202723;color:#dce8e2}（暗い背景に
    # 明るい文字）。カードでは背景を明るくするため、文字色も必ず上書きする。
    # color を省くと「ほぼ白地に明るいグレー文字」となりコントラスト比が
    # 1.13:1 まで落ち、本文が読めなくなる（WCAG基準は4.5:1）。
    pre = (
        "white-space:pre-wrap;background:#f0f3f7;color:#1f2933;"
        "border-radius:6px;padding:8px;font-size:12px;margin:0;"
    )
    cards = []
    for shot in shots:
        cut_id = int(shot.get("id", shot.get("cut_id", 0)))
        cut = cuts.get(cut_id, {})
        asset = shot.get("asset", {}) or {}
        source_rel, clip_rel = _cut_media_paths(state, cut_id)
        request = requests.get(str(cut_id), {}) or {}
        qa = qa_results.get(str(cut_id), {}) or {}
        head = (
            f"<div style='display:flex;align-items:baseline;gap:8px;"
            f"flex-wrap:wrap;'><strong style='font-size:14px;'>Cut {cut_id}</strong>"
            f"<span style='font-size:13px;'>{html.escape(str(cut.get('name', '')))}</span>"
            f"<span style='font-size:11px;color:#8b95a1;'>"
            f"{html.escape(str(cut.get('seconds', '')))}秒 / "
            f"{html.escape(str(cut.get('time_of_day', '')))}</span></div>"
        )

        if phase == "director":
            # 選んだ写真と、その写真に与える指示を並べる
            media = (
                f"<img src='{html.escape(source_rel)}' "
                "style='width:100%;border-radius:8px;display:block;'>"
                if source_rel
                else "<span style='color:#8b95a1;font-size:12px;'>元画像なし</span>"
            )
            cards.append(
                f"<div style='{box}'>{head}"
                "<div style='display:grid;grid-template-columns:220px minmax(0,1fr);"
                "gap:14px;margin-top:10px;'>"
                f"<div>{media}"
                f"<p style='{label}'>使用素材</p>"
                f"<div style='font-size:12px;'>{html.escape(str(asset.get('title', '')))}"
                f" <code>{html.escape(str(asset.get('asset_id', '')))}</code></div>"
                "</div><div>"
                f"<p style='{label}'>カメラワーク</p>"
                f"<div style='font-size:13px;'>"
                f"{html.escape(str(shot.get('camera_motion', '—')))}</div>"
                f"<p style='{label}'>生成プロンプト</p>"
                f"<pre style='{pre}'>"
                f"{html.escape(str(shot.get('positive_prompt', '')))}</pre>"
                f"<p style='{label}'>避ける表現</p>"
                f"<pre style='{pre}'>"
                f"{html.escape(str(shot.get('negative_prompt', '') or '—'))}</pre>"
                f"<p style='{label}'>この演出を選んだ理由</p>"
                f"<div style='font-size:12px;'>"
                f"{html.escape(str(shot.get('rationale', '') or '—'))}</div>"
                "</div></div></div>"
            )
        else:
            # 元画像 → 生成映像 を並べ、生成条件とQA結果を添える
            left = (
                f"<img src='{html.escape(source_rel)}' "
                "style='width:100%;border-radius:8px;display:block;'>"
                if source_rel
                else "<span style='color:#8b95a1;font-size:12px;'>元画像なし</span>"
            )
            right = (
                f"<video src='{html.escape(clip_rel)}' controls "
                "style='width:100%;border-radius:8px;display:block;background:#000;'>"
                "</video>"
                if clip_rel
                else "<span style='color:#8b95a1;font-size:12px;'>未生成</span>"
            )
            verdict = str(qa.get("verdict", "—"))
            issues = qa.get("issues", [])
            issue_text = (
                "<br>".join(
                    html.escape(f"{i.get('code')}: {i.get('description')}")
                    for i in issues
                )
                or "検出された問題はありません"
            )
            cards.append(
                f"<div style='{box}'>{head}"
                "<div style='display:grid;grid-template-columns:1fr 1fr;"
                "gap:12px;margin-top:10px;'>"
                f"<div><p style='{label}'>元画像</p>{left}</div>"
                f"<div><p style='{label}'>生成された映像</p>{right}</div>"
                "</div>"
                f"<p style='{label}'>生成条件</p>"
                "<div style='font-size:12px;color:#5b6570;'>"
                f"{html.escape(_generation_conditions(request, qa))} / "
                f"seed {html.escape(str(request.get('seed', '—')))} / "
                f"試行 {html.escape(str(attempts.get(str(cut_id), 1)))}回目</div>"
                f"<p style='{label}'>QA結果: {html.escape(verdict)}</p>"
                f"<div style='font-size:12px;color:#5b6570;'>{issue_text}</div>"
                "</div>"
            )
    title = "選定した写真と演出指示" if phase == "director" else "生成された映像"
    # .actual-row は grid-template-columns:180px 1fr。カードを直下に並べると
    # 「見出し・カード1」で1行目が埋まり、カード2以降が180pxの狭い列へ
    # 送られて潰れる。カード全体を1つの器に入れ、行の子要素を常に2つに保つ。
    return (
        f"<div class='actual-row'><h4>{title}</h4>"
        f"<div class='card-stack'>{''.join(cards)}</div>"
        "</div>"
    )


def _run_summary_html(state: WorkflowState) -> str:
    """実行サマリー（時間・費用・人間の介入）をレポート末尾に置く。

    審査側が最初に知りたいのは「いくらで、どれだけの時間で、人がどれだけ
    手を入れて作ったか」であり、フェーズ本文を全部読まないと分からない状態を
    避ける。数値はすべてこの run の実績（見積ではない）。
    """
    timing_summary = timing.summarize(state)
    llm = _llm_usage_totals(state)
    video = _video_cost_summary(state)
    human = _human_intervention_summary(state)
    total_cost = llm["cost_usd"] + video["spent_usd"]
    titles = {g["id"]: g["title"] for g in _PHASE_PRESENTATION}
    numbers = {g["id"]: g["number"] for g in _PHASE_PRESENTATION}
    # 図に出さない内部ノードも時間は計測される。phase名のまま出すと
    # 読み手が「これは何の工程か」を判断できないので、日本語名を与える。
    titles.setdefault("commit_cut_qa", "カット判定の確定（内部処理）")

    def minutes(seconds: float) -> str:
        seconds = float(seconds or 0)
        return (
            f"{seconds:.1f}秒"
            if seconds < 60
            else f"{int(seconds // 60)}分{seconds % 60:.0f}秒"
        )

    kpis = [
        ("総所要時間（処理のみ）", minutes(timing_summary["total_phase_seconds"])),
        ("総費用", f"${total_cost:.2f}"),
        (
            "人間の介入",
            f"承認{human['approve']} / 差し戻し{human['retry_with_feedback']}"
            + (f" / 上書き{human['override']}" if human["override"] else ""),
        ),
        ("最も時間を要した工程", str(timing_summary.get("slowest_phase") or "—")),
    ]
    kpi_html = "".join(
        "<div class='kpi'>"
        f"<span class='kpi-label'>{html.escape(label)}</span>"
        f"<strong class='kpi-value'>{html.escape(value)}</strong></div>"
        for label, value in kpis
    )

    # timing.summarize は所要時間の降順で返すが、レポートでは工程の実行順に
    # 並べる（01→02→…）。番号を持たない内部ノードは末尾へ回す。
    order = {guide["id"]: index for index, guide in enumerate(_PHASE_PRESENTATION)}
    phases_in_flow_order = sorted(
        timing_summary.get("phases", []),
        key=lambda row: order.get(row.get("phase"), len(order)),
    )
    phase_rows = "".join(
        "<tr>"
        f"<td>{html.escape(numbers.get(row['phase'], '—'))}</td>"
        f"<td>{html.escape(titles.get(row['phase'], row['phase']))}</td>"
        f"<td class='num'>{html.escape(str(row.get('runs', 1)))}</td>"
        f"<td class='num'>{html.escape(minutes(row.get('cumulative_duration_seconds', 0)))}</td>"
        f"<td>{html.escape(str(row.get('last_status', '')))}</td>"
        "</tr>"
        for row in phases_in_flow_order
    ) or "<tr><td colspan='5'>計測データがありません</td></tr>"

    def video_row(gen: dict[str, Any]) -> str:
        seconds = float(gen.get("billed_seconds", 0) or 0)
        model = str(gen.get("model") or gen.get("provider") or "—")
        job = str(gen.get("job_id") or "—")[:8]
        return (
            "<tr>"
            f"<td class='num'>Cut {html.escape(str(gen.get('cut_id')))}</td>"
            f"<td>{html.escape(model)}</td>"
            f"<td class='num'>{seconds:g}秒</td>"
            f"<td class='num'>${float(gen.get('cost_usd', 0)):.2f}</td>"
            f"<td class='mono'>{html.escape(job)}</td>"
            "</tr>"
        )

    video_rows = "".join(
        video_row(gen) for gen in video["generations"]
    ) or "<tr><td colspan='5'>課金を伴う生成はありません</td></tr>"

    token_note = (
        f"AI（LLM）: {llm['calls']}回の呼び出し / "
        f"{llm['total_tokens']:,}トークン / ${llm['cost_usd']:.4f}"
        f"（{html.escape(str(llm['model']))}）"
        if llm["available"]
        else "AI（LLM）: このrunではトークン使用量が記録されていません"
    )
    video_note = f"動画生成: ${video['spent_usd']:.2f}（実課金）"

    return (
        "<section class='phase-card run-summary-card'>"
        "<h2>実行サマリー</h2>"
        f"<div class='kpi-grid'>{kpi_html}</div>"
        "<h3>工程別の所要時間</h3>"
        "<table class='summary-table'><thead><tr>"
        "<th>#</th><th>工程</th><th>実行回数</th><th>所要時間</th><th>状態</th>"
        f"</tr></thead><tbody>{phase_rows}</tbody></table>"
        "<h3>動画生成の実課金</h3>"
        "<table class='summary-table'><thead><tr>"
        "<th>カット</th><th>モデル</th><th>課金尺</th><th>費用</th><th>Job</th>"
        f"</tr></thead><tbody>{video_rows}</tbody></table>"
        "<h3>費用の内訳</h3>"
        f"<p class='cost-line'>{token_note}</p>"
        f"<p class='cost-line'>{video_note}</p>"
        f"<p class='cost-total'>合計 ${total_cost:.2f}</p>"
        "<p class='muted'>総所要時間は各工程の処理時間の合計であり、"
        "承認画面での待ち時間は含みません。</p>"
        "</section>"
    )


def _process_html(state: WorkflowState, video_name: str) -> str:
    phase_results = state.get("phase_results", {})
    reviews = state.get("reviews", [])
    flow_nodes = []
    cards = []
    for static_guide in _PHASE_PRESENTATION:
        # 実際に使ったバックエンドに合わせて説明文を差し替える
        guide = _guide_for_backend(static_guide, state)
        phase = guide["id"]
        result = phase_results.get(phase, {})
        status = str(result.get("status", "review-only"))
        flow_nodes.append(
            "<div class='flow-node'>"
            f"<span>{html.escape(guide['number'])}</span>"
            f"<strong>{html.escape(guide['title'])}</strong>"
            f"<small>{html.escape(guide['kind'])}</small>"
            "</div>"
        )
        # 演出設計と映像生成は、文字列の羅列では判断できないため
        # 実物（元画像・生成映像）を並べたカードを先頭に置く。
        cards_html = _phase_visual_cards(phase, state) if result else ""
        covered = _CARD_COVERED_ITEMS.get(phase, set()) if cards_html else set()
        actual = cards_html + "".join(
            "<div class='actual-row'>"
            f"<h4>{html.escape(label)}</h4>"
            f"{_render_html_value(value)}"
            "</div>"
            for label, value in (
                (
                    _phase_actual_items(phase, result, state)
                    + _feedback_actual_items(result)
                )
                if result
                else []
            )
            # カードが同じ内容を示す項目は重複するので出さない
            if label not in covered
        )
        related_reviews = [
            review
            for review in reviews
            if review.get("phase") == phase
            or review.get("source_phase") == phase
        ]
        review_html = "".join(
            "<div class='review-row'>"
            f"<strong>{html.escape(str(review.get('action')))}</strong>"
            f"<span>{html.escape(str(review.get('decided_by')))}</span>"
            + (
                f"<p>{html.escape(str(review.get('feedback')))}</p>"
                if review.get("feedback")
                else ""
            )
            + "</div>"
            for review in related_reviews
        ) or "<p class='muted'>この工程の承認記録はありません。</p>"
        technical = {
            "status": result.get("status"),
            "attempt": result.get("attempt"),
            "confidence": result.get("confidence"),
            "warnings": result.get("warnings", []),
            "blocking_issues": result.get("blocking_issues", []),
            "artifacts": result.get("artifacts", []),
        }
        payload = html.escape(
            _portable_path(
                json.dumps(
                    deterministic._sanitized(technical),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        )
        actual_html = (
            actual
            if actual
            else "<p class='muted'>構造化成果物はありません。</p>"
        )
        cards.append(
            "<article class='phase-card'>"
            "<header>"
            f"<span class='phase-number'>{html.escape(guide['number'])}</span>"
            "<div>"
            f"<h2>{html.escape(guide['title'])}</h2>"
            f"<span class='tag'>{html.escape(guide['kind'])}</span>"
            f"<span class='status status-{html.escape(status)}'>"
            f"{html.escape(status)}</span>"
            "</div></header>"
            "<section class='purpose'>"
            "<h3>何のためのノードか</h3>"
            f"<p>{html.escape(guide['purpose'])}</p></section>"
            "<section class='contract-grid'>"
            "<div><h3>入力</h3>"
            f"<p class='source'>入力元: {html.escape(guide['input_source'])}</p>"
            "<ul>"
            + "".join(
                f"<li>{html.escape(item)}</li>"
                for item in guide["inputs"]
            )
            + "</ul></div>"
            "<div><h3>処理</h3>"
            f"<p>{html.escape(guide['process'])}</p></div>"
            "<div><h3>出力</h3>"
            f"<p><code>{html.escape(guide['output'])}</code></p>"
            f"<p class='source'>次: {html.escape(guide['next'])}</p></div>"
            "</section>"
            "<section class='actual'>"
            "<h3>この実行で生成・判断された内容</h3>"
            f"<p class='run-summary'>{html.escape(str(result.get('summary', 'Review Gateとして実行')))}</p>"
            f"{actual_html}"
            "</section>"
            "<section class='reviews'><h3>承認・修正履歴</h3>"
            f"{review_html}</section>"
            "<details class='technical'><summary>技術情報</summary>"
            f"<pre>{payload}</pre></details>"
            "</article>"
        )
    arrows = "<span class='flow-arrow'>→</span>".join(flow_nodes)
    summary_section = _run_summary_html(state)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGEWEC Process Report</title>
<style>
*{{box-sizing:border-box}}body{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;
max-width:1180px;margin:0 auto;padding:34px 22px 80px;background:#f4f3ef;
color:#1c2522;line-height:1.65}}h1,h2,h3,h4,p{{margin-top:0}}
.hero,.workflow,.phase-card{{background:#fff;border:1px solid #d9ddd8;
border-radius:18px;box-shadow:0 8px 28px rgba(20,42,34,.06)}}
.hero{{padding:28px;margin-bottom:24px}}.hero-grid{{display:grid;
grid-template-columns:1.5fr 1fr;gap:26px;align-items:center}}
.eyebrow{{color:#0b7257;font-weight:800;letter-spacing:.08em;
text-transform:uppercase}}video{{width:100%;max-height:460px;background:#000;
border-radius:12px}}.workflow{{padding:24px;margin-bottom:28px}}
.flow{{display:flex;align-items:center;gap:9px;overflow-x:auto;padding:10px 0 16px}}
.flow-node{{min-width:142px;padding:12px;border:1px solid #cdd8d3;
border-radius:12px;background:#f7fbf9;display:grid;gap:3px}}
.flow-node span,.phase-number{{font-weight:900;color:#0b7257}}
.flow-node small{{color:#68736e}}.flow-arrow{{font-size:22px;color:#799087}}
.loop-note{{background:#edf7f3;border-left:4px solid #0b7257;
padding:12px 15px;border-radius:8px;margin:0}}
.phase-card{{padding:24px;margin:18px 0}}.phase-card>header{{display:flex;
gap:16px;align-items:flex-start;border-bottom:1px solid #e5e8e5;padding-bottom:16px}}
.phase-number{{font-size:26px;min-width:56px}}.phase-card h2{{margin-bottom:5px}}
.tag,.status{{display:inline-block;padding:3px 9px;border-radius:999px;
font-size:12px;font-weight:750;margin-right:6px}}.tag{{background:#e9f3ef;color:#155d49}}
.status{{background:#eceeec;color:#58615e}}.status-success{{background:#dff5e9;
color:#11613e}}.status-error{{background:#fde6e3;color:#a1332b}}
.purpose{{padding:18px 0 2px}}.contract-grid{{display:grid;
grid-template-columns:1fr 1fr 1fr;gap:14px;margin:14px 0 22px}}
.contract-grid>div{{background:#f7f7f4;border-radius:12px;padding:16px}}
.contract-grid h3,.actual h3,.reviews h3,.purpose h3{{font-size:15px;
color:#52615b;margin-bottom:7px}}.source,.muted{{color:#74807b}}
.actual{{border-top:1px solid #e5e8e5;padding-top:20px}}.run-summary{{
font-size:18px;font-weight:700}}.actual-row{{display:grid;
grid-template-columns:180px 1fr;gap:16px;padding:10px 0;border-bottom:1px dashed #dde2de}}
.card-stack{{min-width:0}}
.run-summary-card{{padding:26px 28px;margin-top:26px}}
.run-summary-card h2{{font-size:20px;margin-bottom:16px}}
.run-summary-card h3{{font-size:14px;color:#51625b;margin:22px 0 8px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px}}
.kpi{{background:#f4f6f4;border:1px solid #e2e6e2;border-radius:10px;
padding:12px 14px}}
.kpi-label{{display:block;font-size:11px;color:#5b6570;margin-bottom:4px}}
.kpi-value{{font-size:17px;color:#1c2522}}
.summary-table{{width:100%;border-collapse:collapse;font-size:13px}}
.summary-table th{{text-align:left;color:#5b6570;font-weight:600;
font-size:11px;border-bottom:1px solid #dde2de;padding:6px 8px}}
.summary-table td{{border-bottom:1px solid #eef1ee;padding:6px 8px}}
.summary-table td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.summary-table td.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
color:#5b6570}}
.cost-line{{font-size:13px;margin:4px 0;color:#3a463f}}
.cost-total{{font-size:16px;font-weight:700;margin:10px 0 0}}
.actual-row h4{{font-size:14px;color:#51625b;margin:0}}ul{{margin:6px 0;
padding-left:21px}}dl{{display:grid;grid-template-columns:minmax(120px,.35fr) 1fr;
gap:6px 14px;margin:0}}dt{{font-weight:700}}dd{{margin:0}}code{{background:#ecefeb;
padding:3px 6px;border-radius:5px}}.reviews{{padding-top:20px}}
.review-row{{display:grid;grid-template-columns:140px 100px 1fr;gap:10px;
padding:10px 12px;background:#f7fbf9;border-radius:9px;margin:7px 0}}
.review-row p{{margin:0}}.technical{{margin-top:18px}}summary{{font-weight:700;
cursor:pointer;color:#61706a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;
background:#202723;color:#dce8e2;padding:14px;border-radius:9px;font-size:12px}}
.footer{{margin-top:30px;color:#68736e}}
@media(max-width:760px){{.hero-grid,.contract-grid{{grid-template-columns:1fr}}
.actual-row{{grid-template-columns:1fr}}.review-row{{grid-template-columns:1fr}}
body{{padding:18px 12px 50px}}}}
</style></head><body>
<section class="hero"><div class="hero-grid"><div>
<p class="eyebrow">Traceable AI Production</p>
<h1>AGEWEC 制作プロセス</h1>
<p>AIが何を受け取り、なぜ判断し、何を次工程へ渡したかを、
最終動画と一緒に追跡できるレポートです。</p>
<p>Run ID: <code>{html.escape(str(state.get('run_id')))}</code><br>
目標尺: {html.escape(str(state.get('project', {}).get('target_duration_seconds')))}秒</p>
</div><video controls src="{html.escape(video_name)}"></video></div></section>
<section class="workflow"><h2>全体ワークフロー</h2>
<p>左から右へ成果物が受け渡されます。各ノード後のReview Gateは、
設定に応じて人間またはポリシーが承認します。</p>
<div class="flow">{arrows}</div>
<p class="loop-note"><strong>修正ループ:</strong> Cut QAは問題に応じて
Image / Video Production、Support Video Creator、Director、Asset Curatorへ戻り、
Review Boardの修正はPost Productionへ戻ります。</p></section>
<main>{''.join(cards)}</main>
{summary_section}
<p class="footer">公開可能な入力、構造化出力、判断理由、承認履歴を掲載しています。
内部Chain-of-Thought、APIキー、AIサーバーの生ログは掲載しません。
完全な機械可読証跡はprovenance.jsonに保存されています。</p>
</body></html>"""


def _sha256_of(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _copy_cut_sources(state: WorkflowState, package: Path) -> list[dict]:
    """使用元画像（縮小）とカット別動画をPackageへコピーし、索引を返す。

    元画像は最大20MB規模のためPackage肥大化を避けて長辺1280pxへ縮小する。
    追跡性は asset_id / source_url / sha256（原本のもの）で担保する。
    """
    shots = {
        int(s.get("id", s.get("cut_id", 0))): s
        for s in state.get("phase_results", {})
        .get("director", {})
        .get("data", {})
        .get("shots", [])
    }
    artifacts = state.get("production_artifacts", {})
    src_dir = package / "artifacts" / "sources"
    clip_dir = package / "artifacts" / "cuts"
    index: list[dict[str, Any]] = []

    for cut_id in sorted(set(shots) | {int(k) for k in artifacts}):
        shot = shots.get(cut_id, {})
        asset = shot.get("asset", {}) or {}
        entry: dict[str, Any] = {
            "cut_id": cut_id,
            "asset_id": asset.get("asset_id"),
            "title": asset.get("title"),
            "source_url": asset.get("source_url"),
            "detail_url": asset.get("detail_url"),
            "original_local_path": asset.get("local_path"),
            "original_sha256": asset.get("sha256"),
            "selection_reason": asset.get("selection_reason"),
        }
        original = Path(str(asset.get("local_path") or ""))
        if original.exists():
            if not entry["original_sha256"]:
                entry["original_sha256"] = _sha256_of(original)
            src_dir.mkdir(parents=True, exist_ok=True)
            preview = src_dir / f"cut_{cut_id:02d}_source{original.suffix}"
            try:
                downscale_image(str(original), str(preview), max_edge=1280)
            except Exception:  # noqa: BLE001 - 縮小失敗時は原本をコピー
                shutil.copy2(original, preview)
            entry["preview_path"] = str(preview.relative_to(package))

        clip = Path(str((artifacts.get(str(cut_id)) or {}).get("path") or ""))
        if clip.exists():
            clip_dir.mkdir(parents=True, exist_ok=True)
            destination = clip_dir / f"cut_{cut_id:02d}{clip.suffix}"
            shutil.copy2(clip, destination)
            entry["clip_path"] = str(destination.relative_to(package))
            entry["clip_sha256"] = _sha256_of(destination)
        index.append(entry)

    _json_write(package / "cut_sources.json", {"cuts": index})
    return index


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
        "phase_timings": state.get("phase_timings", {}),
        "timing_summary": timing.summarize(state),
    }
    _json_write(package / "provenance.json", provenance)
    _json_write(package / "timing_report.json", timing.summarize(state))

    # run 直下にも実行履歴を残す（提出Packageとは別に、作業用の記録として）
    run_dir = deterministic._work_path(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    _json_write(run_dir / "state.json", provenance)
    events_file = run_dir / "events.jsonl"
    events_file.write_text(
        "\n".join(
            json.dumps(event, ensure_ascii=False)
            for event in state.get("events", [])
        ),
        encoding="utf-8",
    )
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

    # 使用した元画像（レビュー用に縮小）とカット別の動画をPackageへ含める。
    # 元画像の asset_id / source_url / sha256 は原本の証跡として必ず記録する。
    source_index = _copy_cut_sources(state, package)


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
        package / "cut_sources.json",
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
