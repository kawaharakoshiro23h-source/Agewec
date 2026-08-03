"""Backend-native request contracts and paid retry safety tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agewec_v2.pipeline_runtime as runtime


def _shot(image_path: str) -> dict:
    return {
        "id": 1,
        "name": "夜景",
        "seconds": 5.0,
        "media_requirement": "video_required",
        "positive_prompt": "cinematic night view",
        "negative_prompt": "distortion",
        "camera_motion": "slow pan",
        "motion_intensity": "subtle",
        "asset": {"local_path": image_path},
    }


def _base_state(directory: str, image_path: str, backend: str) -> dict:
    return {
        "run_id": "run-contract",
        "phase_results": {"director": {"data": {"shots": [_shot(image_path)]}}},
        "production_requests": {},
        "production_artifacts": {},
        "cut_qa_results": {},
        "cut_results": {},
        "approved_cut_ids": [],
        "cut_attempts": {},
        "attempts": {},
        "feedback": {},
        "review_context": {},
        "events": [],
        "artifacts": [],
        "config": {
            "paths": {"work_dir": directory},
            "production": {
                "backend": backend,
                "model": "gen4.5",
                "profile": "draft",
                "profiles": {
                    "draft": {
                        "width": 576,
                        "height": 384,
                        "frames": 49,
                        "steps": 20,
                        "fps": 24,
                    }
                },
                "model_constraints": {
                    "frame_multiple": 8,
                    "frame_offset": 1,
                    "max_frames": 257,
                },
            },
            "runway": {
                "api_key_env": "RUNWAY_API_KEY",
                "ratio": "1280:720",
                "models": {
                    "gen4.5": {
                        "allowed_seconds": list(range(2, 11)),
                        "resolutions": ["1280:720"],
                        "supports_seed": True,
                        "supports_negative_prompt": False,
                        "has_native_audio": False,
                        "cost_per_second_usd": 0.12,
                    }
                },
            },
            "comfy": {"workflow_api_json": "workflows/ltx_i2v_api.json"},
        },
    }


class BackendRequestContractTest(unittest.TestCase):
    def test_runway_contract_does_not_contain_comfy_ltx_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "source.jpg"
            image.write_bytes(b"image")
            state = _base_state(directory, str(image), "runway")
            with patch.dict(os.environ, {"RUNWAY_API_KEY": "test-key"}):
                update = runtime.support_video_creator(state)

        result = update["phase_results"]["support_video_creator"]
        request = update["production_requests"]["1"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(request["request_contract"], "runway_model_native")
        self.assertEqual(request["model"], "gen4.5")
        self.assertEqual(request["requested_seconds"], 5.0)
        self.assertEqual(request["actual_seconds"], 5.0)
        self.assertEqual(request["ratio"], "1280:720")
        self.assertEqual((request["width"], request["height"]), (1280, 720))
        for comfy_only in ("workflow", "model_profile", "frames", "steps", "fps"):
            self.assertNotIn(comfy_only, request)
        self.assertIsNone(result["data"]["frame_rule"])
        self.assertAlmostEqual(result["data"]["cost_estimate"]["total_usd"], 0.6)

    def test_comfy_contract_preserves_ltx_profile_and_frame_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "source.jpg"
            image.write_bytes(b"image")
            state = _base_state(directory, str(image), "comfy")
            update = runtime.support_video_creator(state)

        request = update["production_requests"]["1"]
        self.assertEqual(request["request_contract"], "comfy_ltx")
        self.assertEqual((request["width"], request["height"]), (576, 384))
        self.assertEqual(request["frames"], 121)
        self.assertEqual(request["steps"], 20)
        self.assertEqual(request["fps"], 24)
        self.assertEqual(request["workflow"], "workflows/ltx_i2v_api.json")
        self.assertAlmostEqual(request["actual_seconds"], 5.0417)

    def test_runway_qa_uses_actual_api_settings_not_stale_comfy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = {
                "run_id": "run-runway-qa",
                "current_cut_id": 1,
                "production_requests": {
                    "1": {
                        "cut_id": 1,
                        "backend": "runway",
                        "actual_seconds": 5.0417,
                        "width": 576,
                        "height": 384,
                        "image_path": "/tmp/source.jpg",
                        "seed": 123,
                    }
                },
                "production_artifacts": {
                    "1": {
                        "path": "/tmp/cut.mp4",
                        "backend": "runway",
                        "attempt": 1,
                        "generation": {
                            "billed_seconds": 5.0,
                            "ratio": "1280:720",
                            "settings": {"ratio": "1280:720"},
                        },
                    }
                },
                "cut_results": {},
                "cut_qa_results": {},
                "attempts": {},
                "feedback": {},
                "review_context": {},
                "events": [],
                "artifacts": [],
                "config": {
                    "paths": {"work_dir": directory},
                    "qa": {
                        "duration_tolerance_seconds": 0.25,
                        "representative_frame_count": 1,
                    },
                },
            }
            technical = {
                "duration_seconds": 5.0417,
                "width": 1280,
                "height": 720,
            }
            with patch.object(runtime, "probe_media", return_value=technical), patch.object(
                runtime, "decode_check"
            ), patch.object(
                runtime, "extract_representative_frames", return_value=[]
            ), patch.object(
                runtime.review_page,
                "build_review_page",
                return_value=Path(directory) / "review.html",
            ):
                update = runtime.cut_visual_qa(state)

        qa = update["phase_results"]["cut_visual_qa"]["data"]
        self.assertEqual(qa["verdict"], "pass")
        self.assertEqual(qa["issues"], [])

    def test_unchanged_paid_technical_retry_is_blocked_before_api_call(self) -> None:
        request = {
            "cut_id": 1,
            "backend": "runway",
            "model": "gen4.5",
            "requested_seconds": 5.0,
            "actual_seconds": 5.0,
            "ratio": "1280:720",
            "width": 1280,
            "height": 720,
        }
        state = {
            "run_id": "run-no-recharge",
            "current_cut_id": 1,
            "production_queue": [1],
            "approved_cut_ids": [],
            "production_requests": {"1": request},
            "cut_results": {
                "1": {
                    "production": {"request": dict(request)},
                    "qa": {
                        "verdict": "revise",
                        "issues": [{"code": "RESOLUTION_MISMATCH"}],
                    },
                }
            },
            "phase_results": {},
            "attempts": {},
            "feedback": {},
            "review_context": {},
            "events": [],
            "artifacts": [],
        }

        update = runtime.image_video_production(state)

        result = update["phase_results"]["image_video_production"]
        self.assertEqual(result["status"], "error")
        self.assertIs(result["data"]["api_called"], False)
        self.assertNotIn("cut_attempts", update)


if __name__ == "__main__":
    unittest.main()
