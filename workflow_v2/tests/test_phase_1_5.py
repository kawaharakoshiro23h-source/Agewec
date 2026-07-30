from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agewec_v2 import nodes
from agewec_v2.graph_safe import _support_review_router, build_graph
from agewec_v2.review import _apply_project_updates, resolve_policy


ROOT = Path(__file__).resolve().parents[1]


class PhaseOneToFiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        self.config["llm"] = {"enabled": False}
        self.state = {
            "run_id": "phase-1-5-test",
            "project": {
                **self.config["project"],
                "target_duration_seconds": 40,
            },
            "config": self.config,
            "phase_results": {},
            "attempts": {},
            "feedback": {},
            "review_context": {},
            "reviews": [],
            "events": [],
            "artifacts": [],
            "aborted": False,
        }

    def _apply(self, update: dict) -> None:
        self.state.update(update)

    def _run_phase_one_to_five(self) -> None:
        for function in (
            nodes.executive_producer,
            nodes.creative_director,
            nodes.writer_storyboard,
            nodes.asset_curator,
            nodes.director,
        ):
            self._apply(function(self.state))

    def test_phase_contracts_are_complete_and_connected(self) -> None:
        self._run_phase_one_to_five()
        results = self.state["phase_results"]

        brief = results["executive_producer"]["data"]
        self.assertEqual(brief["target_duration_seconds"], 40)
        self.assertEqual(
            brief["source_project"]["target_duration_seconds"],
            40,
        )

        concept = results["creative_director"]["data"]
        self.assertIn("camera_intent", concept)
        self.assertTrue(concept["camera_intent"]["hard_constraints"])

        storyboard = results["writer_storyboard"]["data"]
        self.assertAlmostEqual(storyboard["total_seconds"], 40)
        self.assertAlmostEqual(
            sum(cut["seconds"] for cut in storyboard["cuts"]),
            40,
        )
        for cut in storyboard["cuts"]:
            self.assertEqual(cut["media_requirement"], "video_required")
            for field in (
                "time_of_day",
                "visual_role",
                "location",
                "subject",
            ):
                self.assertTrue(cut[field])

        manifest = results["asset_curator"]["data"]
        self.assertFalse(manifest["rights_check_required"])
        self.assertEqual(manifest["unassigned_cut_ids"], [])
        self.assertEqual(
            {item["cut_id"] for item in manifest["asset_assignments"]},
            {cut["id"] for cut in storyboard["cuts"]},
        )
        self.assertTrue(
            all(item["primary"] for item in manifest["asset_assignments"])
        )

        plan = results["director"]["data"]
        self.assertEqual(
            {shot["id"] for shot in plan["shots"]},
            {cut["id"] for cut in storyboard["cuts"]},
        )
        self.assertEqual(
            plan["technical_parameters_status"],
            "pending_support_video_creator",
        )
        for shot in plan["shots"]:
            self.assertNotIn("generation_profile", shot)
            self.assertTrue(shot["rationale"])
            self.assertTrue(shot["camera_intent_alignment"])

    def test_targeted_director_retry_locks_other_cuts(self) -> None:
        self._run_phase_one_to_five()
        before = copy.deepcopy(
            self.state["phase_results"]["director"]["data"]["shots"]
        )
        self.state["review_context"] = {
            "director": {
                "target_cut_id": 2,
                "correction_type": "direction",
            }
        }
        self.state["feedback"]["director"] = "Cut 2だけ修正"
        self._apply(nodes.director(self.state))
        after = self.state["phase_results"]["director"]["data"]["shots"]
        self.assertEqual(
            [shot for shot in before if shot["id"] != 2],
            [shot for shot in after if shot["id"] != 2],
        )
        self.assertEqual(
            self.state["phase_results"]["director"]["data"][
                "targeted_revision_cut_id"
            ],
            2,
        )

    def test_structured_project_update_and_h2_routes(self) -> None:
        updated = _apply_project_updates(
            self.state["project"],
            {"target_duration_seconds": 55},
        )
        self.assertEqual(updated["target_duration_seconds"], 55)
        with self.assertRaises(ValueError):
            _apply_project_updates(updated, {"unknown": True})

        self.assertEqual(
            _support_review_router(
                {
                    "review_route": "retry",
                    "review_target_phase": "asset_curator",
                }
            ),
            "guard_asset_curator",
        )
        self.assertEqual(
            _support_review_router(
                {
                    "review_route": "retry",
                    "review_target_phase": "writer_storyboard",
                }
            ),
            "guard_writer_storyboard",
        )

    def test_phase_one_review_applies_structured_duration_update(self) -> None:
        graph = build_graph(checkpointer=MemorySaver())
        thread = {"configurable": {"thread_id": "phase-one-update"}}
        first = graph.invoke(self.state, thread)
        self.assertIn("__interrupt__", first)
        retried = graph.invoke(
            Command(
                resume={
                    "action": "retry_with_feedback",
                    "feedback": "55秒へ変更",
                    "project_updates": {
                        "target_duration_seconds": 55,
                    },
                }
            ),
            thread,
        )
        self.assertIn("__interrupt__", retried)
        self.assertEqual(
            retried["project"]["target_duration_seconds"],
            55,
        )
        self.assertEqual(
            retried["phase_results"]["executive_producer"]["data"][
                "target_duration_seconds"
            ],
            55,
        )

    def test_h2_asset_correction_routes_to_asset_curator(self) -> None:
        graph = build_graph(checkpointer=MemorySaver())
        thread = {"configurable": {"thread_id": "h2-asset-route"}}
        result = graph.invoke(self.state, thread)
        for expected_phase in (
            "executive_producer",
            "creative_director",
            "writer_storyboard",
            "asset_curator",
        ):
            payload = result["__interrupt__"][0].value
            self.assertEqual(payload["phase"], expected_phase)
            result = graph.invoke(
                Command(resume={"action": "approve"}),
                thread,
            )

        self.assertEqual(result["__interrupt__"][0].value["phase"], "director")
        result = graph.invoke(
            Command(resume={"action": "approve"}),
            thread,
        )
        self.assertEqual(
            result["__interrupt__"][0].value["phase"],
            "support_video_creator",
        )
        result = graph.invoke(
            Command(
                resume={
                    "action": "retry_with_feedback",
                    "feedback": "Cut 2の素材だけ変更",
                    "target_cut_id": 2,
                    "correction_type": "asset",
                }
            ),
            thread,
        )
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["phase"], "asset_curator")
        self.assertEqual(
            result["review_context"]["asset_curator"]["target_cut_id"],
            2,
        )

    def test_explicit_review_policy_overrides_any_preset(self) -> None:
        config = copy.deepcopy(self.config)
        config["autonomy_preset"] = "manual"
        config["review_policies"] = {"writer_storyboard": "never"}
        self.assertEqual(
            resolve_policy(config, "writer_storyboard"),
            "never",
        )


if __name__ == "__main__":
    unittest.main()
