from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from agewec_v2.graph import build_graph
from agewec_v2.review import resolve_policy
from agewec_v2.state import PHASES


ROOT = Path(__file__).resolve().parents[1]


class WorkflowV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.config = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        self.config["paths"] = {"runtime_dir": self.runtime.name}
        # The end-to-end fixture intentionally exercises all six fallback cuts.
        self.config["production"]["max_video_cuts_per_run"] = 6

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
            phase: "never" for phase in PHASES
        }
        config["final_submission"]["require_human"] = False
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
        final_output = Path(result["final_output"])
        self.assertTrue(final_output.exists())
        self.assertEqual(final_output.name, "final_video.mp4")
        self.assertEqual(result["approved_cut_ids"], [1, 2, 3, 4, 5, 6])
        post = result["phase_results"]["post_production"]["data"]
        self.assertEqual(post["implementation"], "ffmpeg_executed")
        self.assertEqual(post["technical_qa"]["status"], "pass")
        submission_dir = Path(
            result["phase_results"]["provenance"]["data"][
                "package_dir"
            ]
        )
        for name in (
            "final_video.mp4",
            "manifest.json",
            "provenance.json",
            "process_report.md",
            "process_report.html",
            "decision_log.jsonl",
            "technical_report.json",
            "storyboard.json",
            "direction_plan.json",
        ):
            self.assertTrue(
                (submission_dir / name).exists(),
                msg=f"missing submission artifact: {name}",
            )
        process_html = (
            submission_dir / "process_report.html"
        ).read_text(encoding="utf-8")
        process_markdown = (
            submission_dir / "process_report.md"
        ).read_text(encoding="utf-8")
        for expected in (
            "全体ワークフロー",
            "何のためのノードか",
            "入力元:",
            "この実行で生成・判断された内容",
            "承認・修正履歴",
            "修正ループ",
            "Executive Producer",
            "Support Video Creator",
        ):
            self.assertIn(expected, process_html)
        self.assertIn(
            "ノードごとの入出力と実行結果",
            process_markdown,
        )
        self.assertIn("出力形式:", process_markdown)
        self.assertNotIn("base_url", process_html)
        self.assertEqual(len(result["reviews"]), 16)

    def test_h3_interrupt_is_mandatory_even_in_autonomous_mode(self) -> None:
        config = copy.deepcopy(self.config)
        config["autonomy_preset"] = "custom"
        config["review_policies"] = {
            phase: "never" for phase in PHASES
        }
        config["final_submission"]["require_human"] = True
        result = build_graph().invoke(
            {
                "run_id": "test-h3-required",
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
        self.assertIn("__interrupt__", result)
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["phase"], "final_submission")
        self.assertTrue(payload["require_human"])
        self.assertTrue(Path(payload["final_video"]).exists())
        self.assertEqual(
            payload["final_technical_qa"]["status"],
            "pass",
        )


if __name__ == "__main__":
    unittest.main()
