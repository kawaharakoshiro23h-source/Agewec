"""差し戻しルート [d][s][g][n] の検証ハーネス（本番から完全に独立）。

Cut QA で人間が選ぶ4つの差し戻し先が、実際にグラフ上で正しく動くかを確認する。
本番の 30秒設定・config_llm.yaml・submissions には一切影響しない。

  - カットは1本だけ（max_video_cuts_per_run: 1）
  - 尺は既定2秒・画質はdraft（576×384）のまま
  - 既定は mock バックエンド（数秒で終わる／ComfyUI不要）
  - 出力先は work/route_test/ に隔離

使い方:
    cd /Users/koshiro/Downloads/Agewec

    # 4ルートをまとめて自動検証（推奨・数十秒）
    PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.test_revision_routes --all

    # 1ルートだけ
    PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.test_revision_routes --route d

    # 実ComfyUIで本当に再生成されるかまで見る（遅い）
    PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.test_revision_routes \
        --route n --backend comfy

各ルートで次を検証する:
    1. 選んだ差し戻し先へ実際に遷移したか
    2. その工程が再実行されたか（attempt が増えたか）
    3. 対象カットIDが引き継がれたか
    4. 修正指示が対象工程へ渡ったか
    5. [n] では seed が変わったか（同じ映像の作り直しを防げているか）
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from .graph_safe import build_graph
from .paths import WORKFLOW_ROOT

ROOT = WORKFLOW_ROOT

ROUTES = {
    "d": ("director", "演出・プロンプトを修正"),
    "s": ("asset_curator", "素材を変更"),
    "g": ("support_video_creator", "生成設定を変更"),
    "n": ("image_video_production", "同じ条件で再生成(seed変更)"),
}
CORRECTION = {
    "d": "direction",
    "s": "asset",
    "g": "generation_parameters",
    "n": "regenerate",
}


def _config(backend: str, seconds: float) -> dict[str, Any]:
    """本番configを読み、テスト用に上書きしたコピーを返す（原本は変更しない）。"""
    config = yaml.safe_load(
        (ROOT / "config.yaml").read_text(encoding="utf-8")
    ) or {}
    config["project"] = {
        **config.get("project", {}),
        "target_duration_seconds": seconds,
    }
    # Cut QA だけ人間が判断し、他は自動で通す
    config["autonomy_preset"] = "custom"
    config["review_policies"] = {
        phase: "never" for phase in config.get("review_policies", {})
    }
    config["review_policies"]["cut_visual_qa"] = "always"
    config["review_policies"]["final_submission"] = "never"
    config.setdefault("final_submission", {})["require_human"] = False
    config["production"] = {
        **config.get("production", {}),
        "backend": backend,
        "max_video_cuts_per_run": 1,
        "profile": "draft",
    }
    # 出力を隔離（本番の work/ を汚さない）
    config["paths"] = {
        **config.get("paths", {}),
        "work_dir": "work/route_test",
        "submissions_dir": "work/route_test/submissions",
    }
    config["llm"] = {**config.get("llm", {}), "enabled": False}
    return config


def _initial(run_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": config["project"],
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
        "human_cut_qa_decisions": {},
        "aborted": False,
    }


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK " if ok else "NG "
    print(f"   [{mark}] {label}{f' — {detail}' if detail else ''}")
    return ok


def run_route(key: str, *, backend: str, seconds: float) -> bool:
    route, label = ROUTES[key]
    print(f"\n=== [{key}] {label} → {route} ===")

    config = _config(backend, seconds)
    graph = build_graph(checkpointer=MemorySaver())
    run_id = f"route-{key}-{uuid.uuid4().hex[:6]}"
    thread = {"configurable": {"thread_id": run_id}}

    feedback_text = f"[テスト] {label}"
    result = graph.invoke(_initial(run_id, config), thread)

    before: dict[str, Any] = {}
    guard = 0
    # 最初の Cut QA まで自動承認で進める
    while "__interrupt__" in result and guard < 40:
        guard += 1
        payload = result["__interrupt__"][0].value
        if payload.get("phase") == "cut_visual_qa":
            break
        result = graph.invoke(
            Command(resume={"action": "approve", "feedback": ""}), thread
        )
    if "__interrupt__" not in result:
        print("   [NG ] Cut QA に到達しなかった")
        return False

    cut_id = int(result.get("current_cut_id") or 0)
    before = {
        "attempts": dict(result.get("attempts", {})),
        "seed": (
            result.get("production_requests", {})
            .get(str(cut_id), {})
            .get("seed")
        ),
    }
    print(f"   Cut {cut_id} の QA で「{label}」を指示")

    # 差し戻しを指示し、次の停止点まで進める（＝差し戻し先が実行される）
    result = graph.invoke(
        Command(
            resume={
                "action": "approve",
                "feedback": feedback_text,
                "cut_route": route,
                "correction_type": CORRECTION[key],
            }
        ),
        thread,
    )

    # 差し戻し直後の状態と、記録されたイベントで判定する
    events = result.get("events", [])
    committed = [
        e for e in events
        if e.get("type") == "cut_qa_committed" and e.get("cut_id") == cut_id
    ]
    human_applied = [
        e for e in events if e.get("type") == "human_cut_decision_applied"
    ]

    ok = True
    ok &= _check(
        "人間の判断がAI判定より優先された",
        bool(committed) and committed[0].get("decided_by") == "human",
        f"decided_by={committed[0].get('decided_by') if committed else '—'}",
    )
    ok &= _check(
        "指定した差し戻し先へ振り分けられた",
        bool(committed) and committed[0].get("route") == route,
        f"route={committed[0].get('route') if committed else '—'}",
    )
    after_attempts = result.get("attempts", {})
    ok &= _check(
        f"{route} が再実行された",
        after_attempts.get(route, 0) > before["attempts"].get(route, 0),
        f"{before['attempts'].get(route, 0)} → {after_attempts.get(route, 0)}",
    )
    ok &= _check(
        "修正指示が記録された",
        bool(human_applied)
        and feedback_text in str(human_applied[0].get("feedback", "")),
    )
    if key == "n":
        after_seed = (
            result.get("production_requests", {})
            .get(str(cut_id), {})
            .get("seed")
        )
        ok &= _check(
            "seedが変わった（別の映像になる）",
            before["seed"] != after_seed,
            f"{before['seed']} → {after_seed}",
        )
    ok &= _check(
        "承認済みカットが壊れていない",
        all(
            c not in result.get("failed_cut_ids", [])
            for c in result.get("approved_cut_ids", [])
        ),
    )
    # 検証は済んだので、残りは走らせずに終了する
    if "__interrupt__" in result:
        graph.invoke(
            Command(resume={"action": "abort", "feedback": ""}), thread
        )
    return bool(ok)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="差し戻しルートの検証（本番に影響しません）"
    )
    parser.add_argument("--route", choices=list(ROUTES))
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--backend", choices=["mock", "comfy"], default="mock"
    )
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args()

    keys = list(ROUTES) if args.all or not args.route else [args.route]
    print(
        f"差し戻しルート検証 / backend={args.backend} / "
        f"1カット・{args.seconds}秒・draft / 出力: work/route_test/"
    )

    results = {key: run_route(key, backend=args.backend,
                              seconds=args.seconds) for key in keys}

    print("\n=== 結果 ===")
    for key, ok in results.items():
        print(f"  [{key}] {ROUTES[key][1]:<28} {'PASS' if ok else 'FAIL'}")
    print("\n" + ("すべて成功" if all(results.values()) else "失敗あり"))


if __name__ == "__main__":
    main()
