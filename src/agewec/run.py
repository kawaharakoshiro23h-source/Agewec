"""実行スクリプト（CLI）。

チェックポイントで interrupt が返ったら承認/やり直しを尋ね、Command で再開する。
非対話時（--auto）は全チェックポイントを自動承認して最後まで走る。

  uv run python -m agewec.run             # 対話（承認を尋ねる）
  uv run python -m agewec.run --auto       # 自動承認で通し実行
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from .graph import build_graph

try:
    from dotenv import load_dotenv
    load_dotenv()  # プロジェクト直下の .env を環境変数に読み込む
except ImportError:
    pass  # python-dotenv 未導入でも骨組みは動く


def load_config() -> dict:
    cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
    if cfg_path.exists():
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return {"backend": "local", "max_retries": 2,
            "checkpoints": {"qa": True, "assembly": True}}


def main() -> None:
    auto = "--auto" in sys.argv
    config = load_config()

    graph = build_graph(checkpointer=MemorySaver())
    thread = {"configurable": {"thread_id": "run-1"}}

    init_state = {
        "target_award": config.get("target_award", "夜景賞"),
        "theme": config.get("theme", "北九州の魅力を世界へ"),
        "config": config,
        "log": [],
    }

    result = graph.invoke(init_state, thread)

    # interrupt が返る限り、承認を尋ねて再開
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print(f"\n[チェックポイント] {payload.get('summary')}")
        if auto:
            decision = "approve"
            print("  → 自動承認")
        else:
            ans = input("  承認して続行? [Y/n/r=やり直し]: ").strip().lower()
            decision = "redo" if ans == "r" else ("reject" if ans == "n" else "approve")
        result = graph.invoke(Command(resume=decision), thread)

    print("\n=== 完了 ===")
    print("final_video:", result.get("final_video"))
    print("log 行数:", len(result.get("log", [])))
    print("証跡: work/workflow_log.json")


if __name__ == "__main__":
    main()
