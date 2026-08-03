"""CLI runner for AGEWEC workflow_v2."""
from __future__ import annotations

import argparse
import json
import math
import uuid
from pathlib import Path
from typing import Any

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from . import timing
from .graph_safe import build_graph
from .review_display import (
    changed_field_labels,
    feedback_status_label,
    feedback_source_label,
    review_summary_lines,
)


ROOT = Path(__file__).resolve().parents[1]


def _gate_snapshot_path(payload: dict[str, Any]) -> Path | None:
    run_id = str(payload.get("run_id") or "")
    phase = str(payload.get("phase") or "")
    if not run_id or not phase:
        return None
    paths = payload.get("paths") or {}
    work_dir = Path(str(paths.get("work_dir", "work")))
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir
    runs_dir = str(paths.get("runs_dir", "runs"))
    attempt = int(payload.get("attempt") or 1)
    return (
        work_dir
        / runs_dir
        / run_id
        / "gates"
        / f"{phase}_attempt_{attempt:02d}.json"
    )


def _write_gate_snapshot(payload: dict[str, Any]) -> Path | None:
    path = _gate_snapshot_path(payload)
    if path is None:
        return None
    snapshot = {
        key: payload.get(key)
        for key in (
            "run_id",
            "phase",
            "source_phase",
            "label",
            "attempt",
            "status",
            "summary",
            "confidence",
            "data",
            "previous_data",
            "feedback_received",
            "feedback_origin",
            "feedback_status",
            "feedback_application_evidence",
            "blocking_issues",
            "warnings",
            "artifacts",
        )
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  warning: 成果物JSONを保存できませんでした: {exc}")
        return None
    return path


def _changed_top_level_fields(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[str]:
    if previous is None:
        return []
    return sorted(
        key
        for key in set(previous) | set(current)
        if previous.get(key) != current.get(key)
    )


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or ROOT / "config_llm.yaml"
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _prompt_optional_positive_int(prompt: str) -> int | None:
    """Enterは未指定、それ以外は正の整数になるまで再入力する。"""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        print("  1以上の整数を入力してください（例: 2）。")


def _prompt_optional_positive_float(prompt: str) -> float | None:
    """Enterは未指定、それ以外は正の有限数になるまで再入力する。"""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
        print("  0より大きい秒数を数値で入力してください（例: 30）。")


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
    for line in review_summary_lines(payload):
        print(line)
    data = payload.get("data") or {}
    previous_data = payload.get("previous_data")
    changed = changed_field_labels(previous_data, data)
    if previous_data is not None:
        print("\n  --- 修正結果 ---")
        print("  変更された項目: " + (", ".join(changed) if changed else "変更なし"))
    if payload.get("feedback_received"):
        print(
            f"  {feedback_source_label(payload.get('feedback_origin'))}: "
            f"{payload['feedback_received']}"
        )
        print(
            "  反映状況: "
            + feedback_status_label(payload.get("feedback_status"))
        )
    snapshot = _write_gate_snapshot(payload)
    if snapshot is not None:
        print(f"\n  技術詳細（必要な場合のみ）: file://{snapshot}")
    if payload.get("artifacts"):
        print("  生成ファイル:")
        for artifact in payload["artifacts"]:
            if isinstance(artifact, dict):
                value = artifact.get("path") or artifact.get("url") or artifact
            else:
                value = artifact
            print(f"    - {value}")
    if payload.get("review_page"):
        print(f"  レビュー画面: file://{payload['review_page']}")
    if payload.get("blocking_issues") and payload.get("phase") != "cut_visual_qa":
        print("  解決が必要な問題:")
        for issue in payload["blocking_issues"]:
            print(f"    - {issue}")
    visible_warnings = list(payload.get("warnings") or [])
    if payload.get("phase") == "support_video_creator":
        visible_warnings = [
            warning
            for warning in visible_warnings
            if not str(warning).startswith("概算費用")
        ]
    if visible_warnings:
        print("  注意事項:")
        for warning in visible_warnings:
            print(f"    - {warning}")
    if payload.get("phase") == "cut_visual_qa":
        qa_data = payload.get("data") or {}
        needs_revision = qa_data.get("verdict") == "revise"
        recommended_choice = {
            "director": "d",
            "asset_curator": "s",
            "support_video_creator": "g",
            "image_video_production": "n",
        }.get(qa_data.get("recommended_route"), "n")
        print("\n  --- 次にどうするか選んでください ---")
        if needs_revision:
            print(
                f"    ※ 現在は再生成が必要です。[{recommended_choice}] が推奨です。"
            )
            print("    [y] 問題を承知で承認して次のカットへ")
        else:
            print("    [Enter/Y] 承認して次のカットへ")
        print(
            "    [d] 演出・生成指示を修正して再生成"
            + ("（推奨）" if recommended_choice == "d" else "")
        )
        print(
            "    [s] 元画像を変更して再生成"
            + ("（推奨）" if recommended_choice == "s" else "")
        )
        print(
            "    [g] 動画モデルや生成設定を変更して再生成"
            + ("（推奨）" if recommended_choice == "g" else "")
        )
        print(
            "    [n] 同じ条件でもう一度生成"
            + ("（推奨）" if recommended_choice == "n" else "")
        )
        print("    [a] 中止")
        choice = input("  選択: ").strip().lower()
        cut_routes = {
            "d": ("director", "direction"),
            "s": ("asset_curator", "asset"),
            "g": ("support_video_creator", "generation_parameters"),
            "n": ("image_video_production", "regenerate"),
        }
        if choice == "a":
            return {"action": "abort", "feedback": ""}
        if needs_revision and not choice:
            choice = recommended_choice
        if choice in cut_routes:
            route, correction_type = cut_routes[choice]
            note = input("  修正指示（任意）: ").strip()
            # action は approve のまま。commit_cut_qa が cut_route を読んで
            # 差し戻し先へ振り分ける。
            return {
                "action": "approve",
                "feedback": note,
                "cut_route": route,
                "correction_type": correction_type,
            }
        if needs_revision and choice == "y":
            return {
                "action": "approve",
                "feedback": "",
                "override_verdict": "pass",
                "override_reason": "人間が問題を確認したうえで承認",
            }
        if not needs_revision and choice in {"", "y"}:
            return {"action": "approve", "feedback": ""}
        print("  未知の選択のため、安全のため中止します。")
        return {"action": "abort", "feedback": ""}

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
            duration = _prompt_optional_positive_float(
                "  目標尺を変更する場合は秒数を入力（変更なしはEnter）: "
            )
            if duration is not None:
                decision["project_updates"] = {
                    "target_duration_seconds": duration
                }
        if payload.get("phase") in {
            "asset_curator",
            "director",
            "support_video_creator",
        }:
            cut_id = _prompt_optional_positive_int(
                "  対象カットID（全体修正はEnter）: "
            )
            if cut_id is not None:
                decision["target_cut_id"] = cut_id
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
        "phase_timings": {},
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

    summary = timing.summarize(result)
    if summary["phases"]:
        print("\n=== フェーズ別 所要時間 ===")
        for row in summary["phases"]:
            print(
                f"  {row['phase']:<26} "
                f"{row['cumulative_duration_seconds']:>8.1f}s "
                f"({row['runs']}回)"
            )
        print(f"  {'合計':<26} {summary['total_phase_seconds']:>8.1f}s")

    print("\n=== AGEWEC workflow_v2 完了 ===")
    print("run_id:", run_id)
    print("aborted:", result.get("aborted", False))
    print("current_phase:", result.get("current_phase"))
    print("final_output:", result.get("final_output"))
    print("review_count:", len(result.get("reviews", [])))


if __name__ == "__main__":
    main()
