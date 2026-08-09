from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from agewec_v2.execution_limits import make_execution_guard
from agewec_v2.graph_safe import build_graph
from agewec_v2.state import PHASES


ROOT = Path(__file__).resolve().parents[1]


def autonomous_config() -> dict:
    config = yaml.safe_load(
        (ROOT / "configs/config_local.yaml").read_text(encoding="utf-8")
    )
    config["autonomy_preset"] = "custom"
    # This suite verifies the historical six-cut transition budget.
    config["production"]["max_video_cuts_per_run"] = 6
    config["review_policies"] = {phase: "never" for phase in PHASES}
    config["execution_limits"] = {
        "max_retries_per_phase": 2,
        "phase_retry_overrides": {
            "support_video_creator": 6,
            "image_video_production": 20,
            "cut_visual_qa": 20,
            "visual_qa": 4,
        },
        "max_total_phase_executions": 30,
        "max_runtime_minutes": 60,
        "on_limit": "abort",
        "max_generation_attempts_per_cut": 2,
        "max_total_production_attempts": 20,
    }
    config["final_submission"]["require_human"] = False
    return config


class ExecutionLimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)

    def _config(self) -> dict:
        config = autonomous_config()
        config["paths"] = {"runtime_dir": self.runtime.name}
        return config

    def test_safe_graph_completes_inside_budget(self) -> None:
        config = self._config()
        result = build_graph().invoke(
            {
                "run_id": "safe-complete",
                "project": config["project"],
                "config": config,
                "phase_results": {},
                "attempts": {},
                "feedback": {},
                "reviews": [],
                "events": [],
                "artifacts": [],
                "aborted": False,
            }
        )
        self.assertFalse(result["aborted"])
        self.assertEqual(result["current_phase"], "provenance")
        self.assertEqual(result["transition_count"], 22)

    def test_global_budget_aborts_before_unbounded_execution(self) -> None:
        config = self._config()
        config["execution_limits"]["max_total_phase_executions"] = 3
        result = build_graph().invoke(
            {
                "run_id": "safe-abort",
                "project": config["project"],
                "config": config,
                "phase_results": {},
                "attempts": {},
                "feedback": {},
                "reviews": [],
                "events": [],
                "artifacts": [],
                "aborted": False,
            }
        )
        self.assertTrue(result["aborted"])
        self.assertEqual(result["transition_count"], 3)
        self.assertEqual(result["limit_status"]["phase"], "asset_curator")

    def test_per_phase_retry_budget_is_enforced(self) -> None:
        config = self._config()
        state = {
            "config": config,
            "attempts": {"executive_producer": 3},
            "events": [],
            "transition_count": 10,
        }
        update = make_execution_guard("executive_producer")(state)
        self.assertEqual(update["guard_route"], "abort")
        self.assertIn(
            "最大実行回数3回",
            update["limit_status"]["violations"][0],
        )


if __name__ == "__main__":
    unittest.main()
