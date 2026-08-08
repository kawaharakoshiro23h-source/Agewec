"""Real one-cut LangGraph integration smoke test.

This test proves the following production chain without running the complete
submission workflow:

    Executive Producer -> Creative Director -> Writer / Storyboard
    -> Asset Curator -> Director -> isolate one cut
    -> Support Video Creator -> ComfyUI -> Phase 07A technical QA

The upstream roles still create and validate the complete project plan.  Only
after Director has made its decision do we isolate one selected cut.  Therefore
the image and prompts sent to ComfyUI are traceable to Asset Curator and
Director, while generation remains small enough for a local smoke test.
"""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from langgraph.graph import END, START, StateGraph

from . import nodes_runtime as runtime
from .backends.comfy_runtime import ComfyClient
from .llm.config import LLMSettings
from .paths import WORKFLOW_ROOT
from .state import WorkflowState

ROOT = WORKFLOW_ROOT
UPSTREAM_PHASES: tuple[
    tuple[str, Callable[[WorkflowState], dict[str, Any]]], ...
] = (
    ("executive_producer", runtime.executive_producer),
    ("creative_director", runtime.creative_director),
    ("writer_storyboard", runtime.writer_storyboard),
    ("asset_curator", runtime.asset_curator),
    ("director", runtime.director),
)


class PipelineSmokeState(WorkflowState, total=False):
    smoke_test: dict[str, Any]


def _phase_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _phase_error(phase: str, result: dict[str, Any]) -> RuntimeError:
    issues = result.get("blocking_issues") or []
    detail = "; ".join(str(item) for item in issues) or result.get(
        "summary",
        "unknown error",
    )
    return RuntimeError(f"{phase} failed: {detail}")


def _checked_node(
    phase: str,
    function: Callable[[WorkflowState], dict[str, Any]],
) -> Callable[[WorkflowState], dict[str, Any]]:
    def run(state: WorkflowState) -> dict[str, Any]:
        print(f"[実行] {phase}")
        update = function(state)
        result = update.get("phase_results", {}).get(phase, {})
        if result.get("status") != "success":
            raise _phase_error(phase, result)
        print(f"[完了] {phase}: {result.get('summary', '')}")
        return update

    return run


def _assigned_assets_for_cut(
    state: WorkflowState,
    cut_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _phase_data(state, "asset_curator")
    assignment = next(
        (
            item
            for item in manifest.get("asset_assignments", [])
            if int(item.get("cut_id", 0)) == cut_id
        ),
        None,
    )
    if not assignment:
        raise RuntimeError(
            f"Asset Curator did not produce an assignment for cut {cut_id}"
        )
    choices = [
        assignment["primary"],
        *assignment.get("alternatives", []),
    ]
    return assignment, choices


def isolate_single_cut(
    state: PipelineSmokeState,
    *,
    cut_id: int,
    seconds: float | None,
) -> dict[str, Any]:
    """Keep one real Director shot and reset only downstream runtime state."""
    phase_results = copy.deepcopy(state.get("phase_results", {}))
    direction = copy.deepcopy(_phase_data(state, "director"))
    all_shots = list(direction.get("shots", []))
    selected = next(
        (
            shot
            for shot in all_shots
            if int(shot.get("id", 0)) == cut_id
        ),
        None,
    )
    if selected is None:
        available = [shot.get("id") for shot in all_shots]
        raise RuntimeError(
            f"Director did not produce cut {cut_id}; available={available}"
        )

    assignment, choices = _assigned_assets_for_cut(state, cut_id)
    selected_asset = dict(selected.get("asset") or {})
    selected_asset_id = str(selected_asset.get("asset_id") or "")
    assigned_ids = {str(item.get("asset_id") or "") for item in choices}
    if selected_asset_id not in assigned_ids:
        raise RuntimeError(
            f"Director asset {selected_asset_id!r} was not assigned "
            f"to cut {cut_id} by Asset Curator"
        )
    eligible_cut_ids = {
        int(value)
        for value in selected_asset.get("eligible_cut_ids", [])
    }
    if eligible_cut_ids and cut_id not in eligible_cut_ids:
        raise RuntimeError(
            f"Director selected {selected_asset_id} for cut {cut_id}, "
            f"but shortlist eligibility is {sorted(eligible_cut_ids)}"
        )

    image_path = Path(str(selected_asset.get("local_path") or ""))
    if not image_path.is_file():
        raise RuntimeError(
            f"Selected local image does not exist for cut {cut_id}: "
            f"{image_path}"
        )
    positive_prompt = str(selected.get("positive_prompt") or "").strip()
    if not positive_prompt:
        raise RuntimeError(f"Director positive prompt is empty for cut {cut_id}")

    original_seconds = float(selected["seconds"])
    if seconds is not None:
        if seconds <= 0:
            raise ValueError("seconds must be greater than zero")
        selected["seconds"] = float(seconds)

    direction["shots"] = [selected]
    direction["smoke_test_full_plan_cut_count"] = len(all_shots)
    phase_results["director"]["data"] = direction
    smoke_test = {
        "cut_id": cut_id,
        "full_plan_cut_count": len(all_shots),
        "original_storyboard_seconds": original_seconds,
        "generation_seconds": float(selected["seconds"]),
        "duration_overridden_for_smoke_test": seconds is not None,
        "asset_curator": {
            "primary_asset_id": assignment["primary"].get("asset_id"),
            "assigned_asset_ids": sorted(assigned_ids),
            "selection_reason": selected_asset.get("selection_reason", ""),
        },
        "director": {
            "asset_id": selected_asset_id,
            "image_path": str(image_path),
            "positive_prompt": positive_prompt,
            "negative_prompt": selected.get("negative_prompt", ""),
            "camera_motion": selected.get("camera_motion", ""),
            "rationale": selected.get("rationale", ""),
        },
    }
    print(
        f"[隔離] cut={cut_id}, asset={selected_asset_id}, "
        f"seconds={selected['seconds']}"
    )
    return {
        "phase_results": phase_results,
        "smoke_test": smoke_test,
        "production_requests": {},
        "production_queue": [],
        "current_cut_id": cut_id,
        "generated_cut_ids": [],
        "approved_cut_ids": [],
        "failed_cut_ids": [],
        "cut_attempts": {},
        "cut_results": {},
        "production_artifacts": {},
        "cut_qa_results": {},
    }


def build_smoke_graph(
    *,
    cut_id: int,
    seconds: float | None,
):
    graph = StateGraph(PipelineSmokeState)
    for phase, function in UPSTREAM_PHASES:
        graph.add_node(phase, _checked_node(phase, function))
    graph.add_node(
        "isolate_single_cut",
        lambda state: isolate_single_cut(
            state,
            cut_id=cut_id,
            seconds=seconds,
        ),
    )
    graph.add_node(
        "support_video_creator",
        _checked_node(
            "support_video_creator",
            runtime.support_video_creator,
        ),
    )
    graph.add_node(
        "image_video_production",
        _checked_node(
            "image_video_production",
            runtime.image_video_production,
        ),
    )
    graph.add_node(
        "cut_visual_qa",
        _checked_node("cut_visual_qa", runtime.cut_visual_qa),
    )

    order = [
        phase for phase, _ in UPSTREAM_PHASES
    ] + [
        "isolate_single_cut",
        "support_video_creator",
        "image_video_production",
        "cut_visual_qa",
    ]
    graph.add_edge(START, order[0])
    for source, target in zip(order, order[1:]):
        graph.add_edge(source, target)
    graph.add_edge(order[-1], END)
    return graph.compile()


def initial_state(config: dict[str, Any], run_id: str) -> PipelineSmokeState:
    return {
        "run_id": run_id,
        "project": dict(config.get("project", {})),
        "config": config,
        "phase_results": {},
        "attempts": {},
        "feedback": {},
        "review_context": {},
        "reviews": [],
        "events": [],
        "artifacts": [],
        "production_requests": {},
        "production_queue": [],
        "current_cut_id": None,
        "generated_cut_ids": [],
        "approved_cut_ids": [],
        "failed_cut_ids": [],
        "cut_attempts": {},
        "cut_results": {},
        "production_artifacts": {},
        "cut_qa_results": {},
        "aborted": False,
        "smoke_test": {},
    }


def llm_preflight(config: dict[str, Any]) -> dict[str, Any]:
    settings = LLMSettings.from_sources(config)
    if not settings.enabled:
        raise RuntimeError(
            "LLM is disabled. This test requires real Asset Curator and "
            "Director decisions."
        )
    response = httpx.get(
        f"{settings.base_url}/models",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=min(settings.timeout_seconds, 10.0),
    )
    response.raise_for_status()
    model_ids = [
        str(item.get("id"))
        for item in response.json().get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    if settings.model not in model_ids:
        raise RuntimeError(
            f"Configured LLM model is unavailable: {settings.model}; "
            f"available={model_ids}"
        )
    return {
        "connected": True,
        "provider": settings.provider,
        "base_url": settings.base_url,
        "model": settings.model,
        "available_model_count": len(model_ids),
    }


def comfy_client(config: dict[str, Any]) -> ComfyClient:
    comfy = dict(config.get("comfy", {}))
    comfy.update(config.get("production", {}).get("comfy", {}))
    return ComfyClient(
        base_url=str(comfy.get("base_url", "http://127.0.0.1:8188")),
        workflow_path=ROOT
        / str(
            comfy.get(
                "workflow_api_json",
                "workflows/ltx_i2v_api.json",
            )
        ),
        input_mapping=comfy.get("inputs", {}),
        output_dir=ROOT / "work" / "production",
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )


def _result_checks(
    state: PipelineSmokeState,
    *,
    cut_id: int,
) -> dict[str, bool]:
    smoke = state["smoke_test"]
    director = smoke["director"]
    request = state["production_requests"][str(cut_id)]
    artifact = state["production_artifacts"][str(cut_id)]
    qa = state["cut_qa_results"][str(cut_id)]
    technical = qa.get("technical", {})
    tolerance = float(
        state.get("config", {})
        .get("qa", {})
        .get("duration_tolerance_seconds", 0.25)
    )
    frames = qa.get("representative_frames", [])
    return {
        "asset_path_reached_request": (
            request["image_path"] == director["image_path"]
        ),
        "positive_prompt_reached_request": (
            request["positive_prompt"] == director["positive_prompt"]
        ),
        "negative_prompt_reached_request": (
            request["negative_prompt"] == director["negative_prompt"]
        ),
        "generated_mp4_exists": Path(artifact["path"]).is_file(),
        "qa_verdict_pass": qa.get("verdict") == "pass",
        "qa_issue_class_pass": qa.get("issue_class") == "pass",
        "duration_matches": (
            abs(
                float(technical.get("duration_seconds", 0))
                - float(request["actual_seconds"])
            )
            <= tolerance
        ),
        "resolution_matches": (
            int(technical.get("width", 0)) == int(request["width"])
            and int(technical.get("height", 0)) == int(request["height"])
        ),
        "fps_matches": (
            abs(
                float(technical.get("fps", 0))
                - float(request["fps"])
            )
            <= 0.01
        ),
        "frame_count_matches": (
            int(technical.get("frame_count", 0))
            == int(request["frames"])
        ),
        "representative_frames_exist": (
            len(frames)
            == int(
                state.get("config", {})
                .get("qa", {})
                .get("representative_frame_count", 3)
            )
            and all(Path(path).is_file() for path in frames)
        ),
    }


def build_report(
    state: PipelineSmokeState,
    *,
    cut_id: int,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    checks = _result_checks(state, cut_id=cut_id)
    phase_summaries = {
        phase: {
            "status": result.get("status"),
            "summary": result.get("summary"),
            "llm": result.get("llm"),
        }
        for phase, result in state.get("phase_results", {}).items()
        if phase
        in {
            *(name for name, _ in UPSTREAM_PHASES),
            "support_video_creator",
            "image_video_production",
            "cut_visual_qa",
        }
    }
    return {
        "test": "agewec_v2_real_pipeline_one_cut",
        "run_id": state["run_id"],
        "status": "pass" if all(checks.values()) else "fail",
        "orchestration": "LangGraph one-cut integration smoke graph",
        "scope": (
            "LLM planning through Asset Curator and Director, real selected "
            "cut generation, and Phase 07A technical QA"
        ),
        "not_proven": [
            "VLM semantic visual quality evaluation",
            "multi-cut sequence QA",
            "FFmpeg final assembly",
            "audio, narration, and subtitles",
        ],
        "preflight": preflight,
        "smoke_test": state["smoke_test"],
        "production_request": state["production_requests"][str(cut_id)],
        "production_artifact": state["production_artifacts"][str(cut_id)],
        "phase_07a": state["cut_qa_results"][str(cut_id)],
        "checks": checks,
        "phase_summaries": phase_summaries,
    }


def write_report(
    report: dict[str, Any],
    *,
    config: dict[str, Any],
    output: Path | None = None,
) -> Path:
    if output is None:
        work_dir = Path(config.get("paths", {}).get("work_dir", "work"))
        if not work_dir.is_absolute():
            work_dir = ROOT / work_dir
        output = (
            work_dir
            / "pipeline_smoke"
            / str(report["run_id"])
            / "report.json"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Asset Curator/Directorの実判断をComfyUIへ渡し、"
            "Phase 07Aまで検証する1カットLangGraphテスト"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config_llm.yaml",
    )
    parser.add_argument("--cut-id", type=int, default=1)
    parser.add_argument(
        "--seconds",
        type=float,
        default=2.0,
        help=(
            "生成だけに使う短縮尺。Storyboardの元の尺はレポートへ保持。"
            "元の尺を使う場合は --seconds 0"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["draft", "final"],
        default="draft",
    )
    parser.add_argument(
        "--backend",
        choices=["comfy", "mock"],
        default="comfy",
    )
    parser.add_argument("--output-report", type=Path)
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="接続事前確認だけを省略（通常は非推奨）",
    )
    args = parser.parse_args()

    config = yaml.safe_load(
        args.config.read_text(encoding="utf-8")
    ) or {}
    config = copy.deepcopy(config)
    config.setdefault("production", {})["backend"] = args.backend
    config["production"]["profile"] = args.profile
    duration_override = None if args.seconds == 0 else args.seconds
    if duration_override is not None and duration_override <= 0:
        raise SystemExit("--seconds must be 0 or greater than zero")

    print("=== 接続確認 ===")
    preflight: dict[str, Any] = {}
    try:
        if args.skip_preflight:
            preflight["skipped"] = True
        else:
            preflight["llm"] = llm_preflight(config)
            print(
                "[OK] LLM:",
                preflight["llm"]["provider"],
                preflight["llm"]["model"],
            )
            if args.backend == "comfy":
                preflight["comfy"] = comfy_client(config).preflight()
                print(
                    "[OK] ComfyUI:",
                    preflight["comfy"]["node_count"],
                    "nodes",
                )
    except Exception as exc:
        raise SystemExit(
            f"事前確認に失敗: {type(exc).__name__}: {exc}"
        ) from exc

    run_id = f"pipeline-smoke-{uuid.uuid4().hex[:10]}"
    print("\n=== 1カット統合パイプライン ===")
    print("run_id:", run_id)
    print("backend:", args.backend)
    print("cut_id:", args.cut_id)
    print(
        "generation_seconds:",
        "storyboard original"
        if duration_override is None
        else duration_override,
    )

    graph = build_smoke_graph(
        cut_id=args.cut_id,
        seconds=duration_override,
    )
    try:
        result = graph.invoke(initial_state(config, run_id))
        report = build_report(
            result,
            cut_id=args.cut_id,
            preflight=preflight,
        )
        report_path = write_report(
            report,
            config=config,
            output=args.output_report,
        )
    except Exception as exc:
        raise SystemExit(
            f"統合テスト失敗: {type(exc).__name__}: {exc}"
        ) from exc

    print("\n=== 判定 ===")
    for name, passed in report["checks"].items():
        print(f" [{'OK' if passed else 'NG'}] {name}")
    print("status:", report["status"])
    print("video:", report["production_artifact"]["path"])
    print("report:", report_path)
    print(
        "semantic_visual_qa:",
        report["phase_07a"]["visual_evaluation"]["status"],
        "(VLM未接続のため技術QAのみ)",
    )
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
