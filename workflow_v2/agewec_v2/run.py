"""CLI runner for AGEWEC workflow_v2."""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from .graph_safe import build_graph


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or ROOT / "config_llm.yaml"
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _decision_from_user(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("kind") == "execution_limit":
        print("\n[自律実行の安全上限]")
        for violation in payload.get("violations", []):
            print(f"  - {violation}")
        answer = input("  [r]一度だけ継続 / [a or Enter]安全に中止: ").strip().lower()
        if answer == "r":
            return {"action": "continue_once", "feedback": ""}
        return {"action": "abort", "feedback": ""}

    print(f"\n[{payload.get('label')}]")
    print(f"  phase: {payload.get('phase')}")
    print(f"  status: {payload.get('status')}")
    print(f"  summary: {payload.get('summary')}")
    if payload.get("blocking_issues"):
        print("  blocking issues:")
        for issue in payload["blocking_issues"]:
            print(f"    - {issue}")
    if payload.get("warnings"):
        print("  warnings:")
        for warning in payload["warnings"]:
            print(f"    - {warning}")
    answer = input(
        "  [Enter/Y]承認 / [r]フィードバック付き再実行 / [a]中止: "
    ).strip().lower()
    if answer == "a":
        return {"action": "abort", "feedback": ""}
    if answer == "r":
        feedback = input("  修正指示: ").strip()
        decision: dict[str, Any] = {
            "action": "retry_with_feedback",
            "feedback": feedback,
        }
        if payload.get("phase") == "executive_producer":
            duration = input(
                "  目標尺を変更する場合は秒数を入力（変更なしはEnter）: "
            ).strip()
            if duration:
                decision["project_updates"] = {
                    "target_duration_seconds": float(duration)
                }
        if payload.get("phase") in {
            "asset_curator",
            "director",
            "support_video_creator",
        }:
            cut_id = input(
                "  対象カットID（全体修正はEnter）: "
            ).strip()
            if cut_id:
                decision["target_cut_id"] = int(cut_id)
        if payload.get("phase") in {
            "director",
            "support_video_creator",
        }:
            correction_type = input(
                "  修正種別 [direction/asset/storyboard] "
                "（directionはEnter）: "
            ).strip()
            decision["correction_type"] = (
                correction_type or "direction"
            )
        return decision
    return {"action": "approve", "feedback": ""}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true")
    parser.add_argument(
        "--preset",
        choices=["manual", "supervised", "autonomous", "custom"],
    )
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    config = load_config(args.config)
    if args.preset:
        config["autonomy_preset"] = args.preset
        if args.preset != "custom":
            config["review_policies"] = {}

    project = dict(config.get("project", {}))
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    graph = build_graph(checkpointer=MemorySaver())
    thread = {"configurable": {"thread_id": run_id}}
    initial = {
        "run_id": run_id,
        "project": project,
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
    }
    result = graph.invoke(initial, thread)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        if args.auto and not payload.get("require_human", False):
            if payload.get("kind") == "execution_limit":
                print(f"[安全停止] {payload.get('label')} — {payload.get('summary')}")
                decision = {"action": "abort", "feedback": ""}
            else:
                print(f"[自動承認] {payload.get('label')} — {payload.get('summary')}")
                decision = {"action": "approve", "feedback": ""}
        else:
            if args.auto and payload.get("require_human", False):
                print("[人間確認必須] --autoでもH3は自動承認しません。")
            decision = _decision_from_user(payload)
        result = graph.invoke(Command(resume=decision), thread)

    print("\n=== AGEWEC workflow_v2 完了 ===")
    print("run_id:", run_id)
    print("aborted:", result.get("aborted", False))
    print("current_phase:", result.get("current_phase"))
    print("final_output:", result.get("final_output"))
    print("review_count:", len(result.get("reviews", [])))


if __name__ == "__main__":
    main()
