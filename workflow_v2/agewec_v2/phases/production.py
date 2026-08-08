"""Phase 06 video generation and paid-generation guards."""
from __future__ import annotations

import json
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import nodes as deterministic
from ..backends import (
    BudgetStatus,
    ComfyClient,
    ComfyGenerationRequest,
    UnsupportedDurationError,
    VideoCostGuard,
    resolve_backend,
    to_video_request,
)
from ..media_tools import (
    MediaToolError,
    downscale_image,
    generate_mock_video,
    probe_media,
)
from ..paths import runtime_paths
from ..state import WorkflowState

from .common import (
    _attempt_json_path, _json_write, _stable_seed,
)

def _generate_comfy(
    state: WorkflowState,
    request: dict[str, Any],
) -> dict[str, Any]:
    config = state.get("config", {})
    comfy = dict(config.get("comfy", {}))
    comfy.update(config.get("production", {}).get("comfy", {}))
    workflow_path = runtime_paths(config).resolve_workflow(
        str(comfy.get("workflow_api_json", "workflows/ltx_i2v_api.json"))
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
    """H2で提示する、カット別モデルを反映した実行全体の概算費用。"""
    try:
        adapter = _video_backend(state, backend)
    except Exception:  # noqa: BLE001
        return None
    if not requests:
        return None
    lines: list[dict[str, Any]] = []
    models: list[str] = []
    total = 0.0
    for request in requests:
        model = str(request.get("model") or "") or None
        try:
            caps = adapter.capabilities(model)
            requested = float(
                request.get("requested_seconds")
                or request.get("actual_seconds", 0)
            )
            billed = caps.resolve_seconds(requested)
            cost = caps.estimate_cost(requested)
        except (UnsupportedDurationError, ValueError, RuntimeError) as exc:
            return {
                "model": model or "—",
                "models": sorted(set(models + ([model] if model else []))),
                "total_usd": round(total, 4),
                "error": f"Cut {request.get('cut_id')}: {exc}",
            }
        models.append(caps.model)
        total += cost
        lines.append(
            {
                "cut_id": request.get("cut_id"),
                "model": caps.model,
                "requested_seconds": requested,
                "billed_seconds": billed,
                "cost_per_second_usd": caps.cost_per_second_usd,
                "cost_usd": round(cost, 4),
            }
        )
    unique_models = list(dict.fromkeys(models))
    if total <= 0:
        return None
    return {
        "model": " / ".join(unique_models),
        "models": unique_models,
        "cuts": lines,
        "total_usd": round(total, 4),
        "cost_per_second_usd": (
            lines[0]["cost_per_second_usd"]
            if len(unique_models) == 1
            else None
        ),
    }


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
        request.get("resolution"),
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


def _prepare_runway_input_image(
    state: WorkflowState,
    cut_id: int,
    attempt: int,
    request: dict[str, Any],
    output_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Runwayに送る参照画像だけを安全な寸法へ縮小する。

    原本とProductionRequestは変更せず、Runway Adapterへ渡す一時的な
    requestの`image_path`だけを差し替える。Comfyなど他のバックエンド
    からはこの関数を呼ばない。
    """
    runway_config = dict(state.get("config", {}).get("runway", {}))
    image_config = dict(runway_config.get("input_image", {}))
    max_edge = int(image_config.get("max_edge", 4096))
    quality = int(image_config.get("jpeg_quality", 2))
    if max_edge <= 0:
        raise ValueError("runway.input_image.max_edge must be positive")
    if not 1 <= quality <= 31:
        raise ValueError(
            "runway.input_image.jpeg_quality must be between 1 and 31"
        )

    source = Path(str(request.get("image_path") or ""))
    if not source.is_file():
        raise MediaToolError(f"Runway参照画像が存在しない: {source}")
    original = probe_media(source)
    width = int(original.get("width") or 0)
    height = int(original.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaToolError(
            f"Runway参照画像の寸法を取得できない: {source}"
        )

    preparation: dict[str, Any] = {
        "cut_id": cut_id,
        "attempt": attempt,
        "original_path": str(source),
        "original_width": width,
        "original_height": height,
        "original_bytes": int(original.get("bytes") or source.stat().st_size),
        "max_edge": max_edge,
        "resized": False,
        "prepared_path": str(source),
        "prepared_width": width,
        "prepared_height": height,
        "prepared_bytes": int(
            original.get("bytes") or source.stat().st_size
        ),
    }
    if max(width, height) <= max_edge:
        return dict(request), preparation

    destination = output_dir / f"attempt_{attempt:02d}_runway_input.jpg"
    downscale_image(
        source,
        destination,
        max_edge=max_edge,
        quality=quality,
    )
    prepared = probe_media(destination)
    prepared_width = int(prepared.get("width") or 0)
    prepared_height = int(prepared.get("height") or 0)
    if (
        prepared_width <= 0
        or prepared_height <= 0
        or max(prepared_width, prepared_height) > max_edge
    ):
        raise MediaToolError(
            "Runway参照画像の縮小結果が不正: "
            f"{prepared_width}x{prepared_height}, max_edge={max_edge}"
        )
    preparation.update(
        {
            "resized": True,
            "prepared_path": str(destination),
            "prepared_width": prepared_width,
            "prepared_height": prepared_height,
            "prepared_bytes": int(
                prepared.get("bytes") or destination.stat().st_size
            ),
        }
    )
    adapter_request = {**request, "image_path": str(destination)}
    return adapter_request, preparation


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
    input_preparation: dict[str, Any] = {}
    error_record: dict[str, Any] | None = None
    error_path: Path | None = None
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
        adapter_request = request
        if (
            backend == "runway"
            and str(request.get("generation_mode") or "image_to_video")
            == "image_to_video"
        ):
            adapter_request, input_preparation = _prepare_runway_input_image(
                state,
                current,
                attempt,
                request,
                output_dir,
            )
        # 生成本体は必ず Adapter 経由で行う。上のRunway分岐は
        # 課金APIへのアップロード前処理だけを担当する。
        adapter = _video_backend(state, backend)
        result = adapter.generate(
            to_video_request(adapter_request, attempt=attempt)
        )
        output_path = result.output_path
        generation = {
            **result.settings,
            **result.to_record(),
            "output_path": output_path,
            "input_preparation": input_preparation,
        }
    except Exception as exc:
        issue = f"{type(exc).__name__}: {exc}"
        issues.append(issue)
        error_path = _attempt_json_path(
            state,
            current,
            attempt,
            "error",
        )
        error_record = {
            "phase": phase,
            "cut_id": current,
            "attempt": attempt,
            "backend": backend,
            "model": str(request.get("model") or ""),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "request": deterministic._sanitized(request),
            "input_preparation": deterministic._sanitized(
                input_preparation
            ),
        }
        try:
            _json_write(error_path, error_record)
        except OSError as write_exc:
            issues.append(
                "ErrorLogWriteError: "
                f"{type(write_exc).__name__}: {write_exc}"
            )

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
        "error": error_record,
        "error_path": str(error_path) if error_path else None,
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
            "generation_error": error_record,
            "error_path": str(error_path) if error_path else None,
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

