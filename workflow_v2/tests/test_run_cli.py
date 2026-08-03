from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agewec_v2.review_display import review_summary_lines
from agewec_v2.review import _normalize_decision
from agewec_v2.run import (
    DEFAULT_CHECKPOINT_DB,
    _changed_top_level_fields,
    _decision_from_user,
    _resume_command,
    _write_gate_snapshot,
)


class RunCliReviewDisplayTest(unittest.TestCase):
    def test_resume_command_includes_non_default_checkpoint_database(self) -> None:
        default_command = _resume_command(
            "run-test",
            DEFAULT_CHECKPOINT_DB.resolve(),
        )
        custom_command = _resume_command(
            "run-test",
            Path("/tmp/checkpoint db.sqlite"),
        )

        self.assertNotIn("--checkpoint-db", default_command)
        self.assertIn(
            "--checkpoint-db '/tmp/checkpoint db.sqlite'",
            custom_command,
        )

    def test_gate_snapshot_contains_current_and_previous_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "run_id": "run-review-test",
                "phase": "executive_producer",
                "source_phase": "executive_producer",
                "label": "Executive Producer",
                "attempt": 2,
                "status": "success",
                "summary": "brief updated",
                "confidence": 0.9,
                "data": {"audience": "国内旅行者", "duration": 10},
                "previous_data": {
                    "audience": "国内外の旅行者",
                    "duration": 10,
                },
                "feedback_received": "国内旅行者向けに変更",
                "feedback_status": (
                    "delivered_to_llm_pending_human_verification"
                ),
                "feedback_application_evidence": "output_changed",
                "blocking_issues": [],
                "warnings": [],
                "artifacts": [],
                "paths": {"work_dir": directory},
            }

            path = _write_gate_snapshot(payload)

            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.exists())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["data"], payload["data"])
            self.assertEqual(saved["previous_data"], payload["previous_data"])
            self.assertEqual(
                _changed_top_level_fields(
                    payload["previous_data"], payload["data"]
                ),
                ["audience"],
            )

    def test_support_video_summary_hides_internal_generation_fields(self) -> None:
        payload = {
            "phase": "support_video_creator",
            "status": "success",
            "summary": "2カットのProductionRequestを構築",
            "data": {
                "backend": "runway",
                "profile_name": "draft",
                "requests": [
                    {
                        "cut_id": 1,
                        "requested_seconds": 5.0,
                        "image_path": "/tmp/kokura.jpg",
                        "camera_motion": "slow pan",
                        "positive_prompt": "night view",
                        "seed": 123,
                        "frames": 121,
                    }
                ],
                "cost_estimate": {"model": "gen4.5", "total_usd": 0.6},
            },
        }

        rendered = "\n".join(review_summary_lines(payload))

        self.assertIn("生成先: Runway API", rendered)
        self.assertIn("使用モデル: gen4.5", rendered)
        self.assertIn("概算費用: US$0.60", rendered)
        self.assertIn("元画像: kokura.jpg", rendered)
        self.assertNotIn("seed", rendered)
        self.assertNotIn("frames", rendered)

    def test_failed_cut_qa_explains_failure_and_enter_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "kind": "review_gate",
                "run_id": "run-qa-display",
                "phase": "cut_visual_qa",
                "label": "Phase 07A: Cut Visual QA",
                "status": "error",
                "summary": "Cut 1 QA: revise (runtime_transient)",
                "attempt": 1,
                "data": {
                    "cut_id": 1,
                    "attempt": 1,
                    "seed": 123,
                    "artifact_path": None,
                    "verdict": "revise",
                    "issues": [
                        {
                            "code": "MEDIA_TECHNICAL_ERROR",
                            "description": "MediaToolError: 生成動画のArtifactがない",
                        }
                    ],
                    "recommended_route": "image_video_production",
                    "confidence": 0.85,
                    "source_image": "/tmp/kokura.jpg",
                },
                "paths": {"work_dir": directory},
            }
            output = io.StringIO()
            with patch("builtins.input", side_effect=["", ""]):
                with redirect_stdout(output):
                    decision = _decision_from_user(payload)

        rendered = output.getvalue()
        self.assertIn("判定: 再生成が必要", rendered)
        self.assertIn("映像確認: 未実施", rendered)
        self.assertIn("動画生成結果のファイルが作成されなかった", rendered)
        self.assertNotIn('"confidence"', rendered)
        self.assertNotIn('"seed"', rendered)
        self.assertNotIn("runtime_transient", rendered)
        self.assertEqual(decision["cut_route"], "image_video_production")

    def test_failed_cut_qa_shows_the_upstream_generation_error(self) -> None:
        payload = {
            "phase": "cut_visual_qa",
            "status": "error",
            "data": {
                "cut_id": 1,
                "attempt": 1,
                "artifact_path": None,
                "verdict": "revise",
                "issues": [{"code": "MEDIA_TECHNICAL_ERROR"}],
                "generation_error": {
                    "exception_type": "RunwayError",
                    "message": "signed upload timed out",
                },
                "recommended_route": "image_video_production",
                "source_image": "/tmp/kokura.jpg",
            },
        }

        rendered = "\n".join(review_summary_lines(payload))

        self.assertIn("RunwayError", rendered)
        self.assertIn("signed upload timed out", rendered)
        self.assertNotIn("理由: 動画生成結果のファイル", rendered)

    def test_y_explicitly_overrides_failed_qa(self) -> None:
        payload = {
            "kind": "review_gate",
            "phase": "cut_visual_qa",
            "label": "Cut QA",
            "status": "error",
            "summary": "revise",
            "data": {
                "cut_id": 1,
                "verdict": "revise",
                "recommended_route": "support_video_creator",
            },
        }
        with patch("builtins.input", return_value="y"), redirect_stdout(io.StringIO()):
            decision = _decision_from_user(payload)

        self.assertEqual(decision["action"], "approve")
        self.assertEqual(decision["override_verdict"], "pass")
        normalized = _normalize_decision(decision)
        self.assertEqual(normalized["override_verdict"], "pass")

    def test_y_approves_successful_qa_without_override(self) -> None:
        payload = {
            "kind": "review_gate",
            "phase": "cut_visual_qa",
            "label": "Cut QA",
            "status": "success",
            "summary": "pass",
            "data": {"cut_id": 1, "verdict": "pass"},
        }
        with patch("builtins.input", return_value="y"), redirect_stdout(io.StringIO()):
            decision = _decision_from_user(payload)

        self.assertEqual(decision, {"action": "approve", "feedback": ""})

    def test_invalid_duration_reprompts_instead_of_crashing(self) -> None:
        payload = {
            "phase": "executive_producer",
            "label": "Executive Producer",
            "data": {},
        }
        output = io.StringIO()
        answers = ["r", "30秒に変更", "abc", "0", "NaN", "inf", "30"]

        with patch("builtins.input", side_effect=answers), redirect_stdout(output):
            decision = _decision_from_user(payload)

        self.assertEqual(
            decision["project_updates"]["target_duration_seconds"],
            30.0,
        )
        self.assertGreaterEqual(
            output.getvalue().count("0より大きい秒数"),
            4,
        )

    def test_invalid_cut_id_reprompts_instead_of_crashing(self) -> None:
        payload = {
            "phase": "director",
            "label": "Director",
            "data": {},
        }
        output = io.StringIO()
        answers = ["r", "Cut 2だけ修正", "Cut2", "0", "-1", "2.5", "2", ""]

        with patch("builtins.input", side_effect=answers), redirect_stdout(output):
            decision = _decision_from_user(payload)

        self.assertEqual(decision["target_cut_id"], 2)
        self.assertEqual(decision["correction_type"], "direction")
        self.assertGreaterEqual(
            output.getvalue().count("1以上の整数"),
            4,
        )

    def test_blank_optional_values_keep_existing_settings(self) -> None:
        executive = {
            "phase": "executive_producer",
            "label": "Executive Producer",
            "data": {},
        }
        director = {
            "phase": "director",
            "label": "Director",
            "data": {},
        }
        with patch(
            "builtins.input", side_effect=["r", "文言だけ修正", ""]
        ), redirect_stdout(io.StringIO()):
            executive_decision = _decision_from_user(executive)
        with patch(
            "builtins.input", side_effect=["r", "全カット修正", "", ""]
        ), redirect_stdout(io.StringIO()):
            director_decision = _decision_from_user(director)

        self.assertNotIn("project_updates", executive_decision)
        self.assertNotIn("target_cut_id", director_decision)


if __name__ == "__main__":
    unittest.main()
