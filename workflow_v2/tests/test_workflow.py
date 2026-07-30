from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from agewec_v2.graph import build_graph
from agewec_v2.review import resolve_policy


ROOT = Path(__file__).resolve().parents[1]


class WorkflowV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.config = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        )

    def test_review_policy_presets_and_override(self) -> None:
        config = copy.deepcopy(self.config)
        config["autonomy_preset"] = "autonomous"
        config["review_policies"] = {}
        self.assertEqual(
            resolve_policy(config, "executive_producer"),
            "on_exception",
        )
        self.assertEqual(resolve_policy(config, "final_submission"), "always")
        config["autonomy_preset"] = "custom"
        config["review_policies"]["director"] = "never"
        self.assertEqual(resolve_policy(config, "director"), "never")

    def test_graph_runs_without_interrupts_when_all_reviews_never(self) -> None:
        config = copy.deepcopy(self.config)
        config["autonomy_preset"] = "custom"
        config["review_policies"] = {
            phase: "never"
            for phase in (
                "executive_producer",
                "creative_director",
                "writer_storyboard",
                "asset_curator",
                "director",
                "image_video_production",
                "visual_qa",
                "post_production",
                "review_board",
                "final_submission",
                "provenance",
            )
        }
        result = build_graph().invoke(
            {
                "run_id": "test-run",
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
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(result["current_phase"], "provenance")
        self.assertIn("provenance", result["phase_results"])
        self.assertTrue(Path(result["final_output"]).exists())
        self.assertEqual(len(result["reviews"]), 11)


if __name__ == "__main__":
    unittest.main()
