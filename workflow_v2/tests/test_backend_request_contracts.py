"""Backend-native request contracts and paid retry safety tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agewec_v2.pipeline_runtime as runtime
from agewec_v2.backends.base import VideoResult
from agewec_v2.backends.runway import RunwayError


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


def _production_state(directory: str, image_path: str, backend: str) -> dict:
    state = _base_state(directory, image_path, backend)
    request = {
        "cut_id": 1,
        "backend": backend,
        "model": "gen4.5",
        "image_path": image_path,
        "positive_prompt": "cinematic night view",
        "negative_prompt": "",
        "requested_seconds": 5.0,
        "actual_seconds": 5.0,
        "width": 1280,
        "height": 720,
        "ratio": "1280:720",
        "seed": 123,
    }
    state.update(
        {
            "current_cut_id": 1,
            "production_queue": [1],
            "production_requests": {"1": request},
        }
    )
    state["config"]["runway"]["input_image"] = {
        "max_edge": 4096,
        "jpeg_quality": 2,
    }
    return state


class _CapturingAdapter:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.request = None

    def generate(self, request):
        self.request = request
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(b"video")
        return VideoResult(
            output_path=str(self.destination),
            provider="runway",
            model="gen4.5",
            requested_seconds=5.0,
            billed_seconds=5.0,
            actual_seconds=5.0,
            settings={"ratio": "1280:720"},
        )


class _FailingAdapter:
    def generate(self, request):
        raise RunwayError("signed upload timed out")


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

    def test_hailuo_contract_uses_2k_without_inventing_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "source.jpg"
            image.write_bytes(b"image")
            state = _base_state(directory, str(image), "runway")
            state["config"]["production"]["model"] = "hailuo3"
            state["config"]["runway"]["models"]["hailuo3"] = {
                "allowed_seconds": list(range(5, 16)),
                "resolutions": ["2K"],
                "resolution": "2K",
                "prompt_image_format": "keyframes",
                "generation_modes": ["image_to_video"],
                "supports_seed": False,
                "supports_negative_prompt": False,
                "has_native_audio": True,
                "cost_per_second_usd": 0.15,
            }
            with patch.dict(os.environ, {"RUNWAY_API_KEY": "test-key"}):
                update = runtime.support_video_creator(state)

        result = update["phase_results"]["support_video_creator"]
        request = update["production_requests"]["1"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(request["model"], "hailuo3")
        self.assertEqual(request["resolution"], "2K")
        self.assertNotIn("ratio", request)
        self.assertNotIn("width", request)
        self.assertNotIn("height", request)
        self.assertAlmostEqual(
            result["data"]["cost_estimate"]["total_usd"], 0.75
        )

    def test_hailuo_text_to_video_is_blocked_at_paid_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _base_state(directory, "", "runway")
            state["config"]["production"]["model"] = "hailuo3"
            state["config"]["runway"]["models"]["hailuo3"] = {
                "allowed_seconds": list(range(5, 16)),
                "resolutions": ["2K"],
                "resolution": "2K",
                "prompt_image_format": "keyframes",
                "generation_modes": ["image_to_video"],
                "supports_seed": False,
                "supports_negative_prompt": False,
                "has_native_audio": True,
                "cost_per_second_usd": 0.15,
            }
            shot = state["phase_results"]["director"]["data"]["shots"][0]
            shot["generation_mode"] = "text_to_video"
            shot["asset"] = None
            update = runtime.support_video_creator(state)

        result = update["phase_results"]["support_video_creator"]
        self.assertEqual(result["status"], "error")
        self.assertTrue(
            any(
                "hailuo3 は現在 text_to_video に未対応" in issue
                for issue in result["blocking_issues"]
            ),
            result["blocking_issues"],
        )
        self.assertNotIn("1", update["production_requests"])

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

    def test_hailuo_qa_records_actual_adaptive_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = {
                "run_id": "run-hailuo-qa",
                "current_cut_id": 1,
                "production_requests": {
                    "1": {
                        "cut_id": 1,
                        "backend": "runway",
                        "model": "hailuo3",
                        "actual_seconds": 5.0,
                        "resolution": "2K",
                        "image_path": "/tmp/source.jpg",
                    }
                },
                "production_artifacts": {
                    "1": {
                        "path": "/tmp/cut.mp4",
                        "backend": "runway",
                        "attempt": 1,
                        "generation": {
                            "billed_seconds": 5.0,
                            "resolution": "2K",
                            "settings": {"resolution": "2K"},
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
                "duration_seconds": 5.0,
                "width": 2048,
                "height": 1365,
            }
            with patch.object(
                runtime, "probe_media", return_value=technical
            ), patch.object(runtime, "decode_check"), patch.object(
                runtime, "extract_representative_frames", return_value=[]
            ), patch.object(
                runtime.review_page,
                "build_review_page",
                return_value=Path(directory) / "review.html",
            ):
                update = runtime.cut_visual_qa(state)

        qa = update["phase_results"]["cut_visual_qa"]["data"]
        self.assertEqual(qa["verdict"], "pass")
        self.assertEqual(qa["technical"]["width"], 2048)
        self.assertEqual(qa["technical"]["height"], 1365)

    def test_large_runway_image_is_resized_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large-source.jpg"
            source.write_bytes(b"original-image")
            state = _production_state(directory, str(source), "runway")
            adapter = _CapturingAdapter(Path(directory) / "generated.mp4")

            def fake_downscale(src, destination, **kwargs):
                self.assertEqual(Path(src), source)
                self.assertEqual(kwargs["max_edge"], 4096)
                Path(destination).write_bytes(b"resized-image")
                return str(destination)

            probes = [
                {
                    "width": 8136,
                    "height": 5424,
                    "bytes": len(b"original-image"),
                },
                {
                    "width": 4096,
                    "height": 2731,
                    "bytes": len(b"resized-image"),
                },
            ]
            with patch.object(
                runtime, "probe_media", side_effect=probes
            ), patch.object(
                runtime, "downscale_image", side_effect=fake_downscale
            ), patch.object(
                runtime, "_video_backend", return_value=adapter
            ):
                update = runtime.image_video_production(state)

            self.assertEqual(source.read_bytes(), b"original-image")
            self.assertIsNotNone(adapter.request)
            prepared = Path(adapter.request.image_path)
            self.assertNotEqual(prepared, source)
            self.assertEqual(prepared.name, "attempt_01_runway_input.jpg")
            self.assertEqual(prepared.read_bytes(), b"resized-image")
            generation = update["production_artifacts"]["1"]["generation"]
            self.assertIs(generation["input_preparation"]["resized"], True)
            self.assertEqual(
                state["production_requests"]["1"]["image_path"],
                str(source),
            )

    def test_comfy_generation_never_uses_runway_image_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            source.write_bytes(b"source-image")
            state = _production_state(directory, str(source), "comfy")
            adapter = _CapturingAdapter(Path(directory) / "generated.mp4")
            with patch.object(
                runtime, "_prepare_runway_input_image"
            ) as prepare, patch.object(
                runtime, "_video_backend", return_value=adapter
            ):
                update = runtime.image_video_production(state)

            prepare.assert_not_called()
            self.assertEqual(
                update["phase_results"]["image_video_production"]["status"],
                "success",
            )
            self.assertEqual(adapter.request.image_path, str(source))

    def test_runway_text_to_video_skips_image_preparation(self) -> None:
        """T2Vは参照画像がないため、Runway画像前処理を呼んではならない。"""
        with tempfile.TemporaryDirectory() as directory:
            state = _production_state(directory, "", "runway")
            state["production_requests"]["1"]["generation_mode"] = (
                "text_to_video"
            )
            adapter = _CapturingAdapter(Path(directory) / "generated.mp4")
            with patch.object(
                runtime, "_prepare_runway_input_image"
            ) as prepare, patch.object(
                runtime, "_video_backend", return_value=adapter
            ):
                update = runtime.image_video_production(state)

            prepare.assert_not_called()
            self.assertEqual(
                update["phase_results"]["image_video_production"]["status"],
                "success",
            )
            self.assertEqual(adapter.request.image_path, "")

    def test_generation_failure_is_persisted_and_reaches_cut_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            source.write_bytes(b"source-image")
            state = _production_state(directory, str(source), "runway")
            source_probe = {
                "width": 1280,
                "height": 720,
                "bytes": source.stat().st_size,
            }
            with patch.object(
                runtime, "probe_media", return_value=source_probe
            ), patch.object(
                runtime, "_video_backend", return_value=_FailingAdapter()
            ):
                update = runtime.image_video_production(state)

            result = update["phase_results"]["image_video_production"]
            self.assertEqual(result["status"], "error")
            error_path = Path(result["data"]["error_path"])
            self.assertTrue(error_path.is_file())
            error = json.loads(error_path.read_text(encoding="utf-8"))
            self.assertEqual(error["exception_type"], "RunwayError")
            self.assertEqual(error["message"], "signed upload timed out")
            self.assertIn("Traceback", error["traceback"])

            qa_state = {**state, **update}
            qa_update = runtime.cut_visual_qa(qa_state)
            qa = qa_update["phase_results"]["cut_visual_qa"]["data"]
            self.assertEqual(qa["verdict"], "revise")
            self.assertEqual(
                qa["generation_error"]["message"],
                "signed upload timed out",
            )
            self.assertIn(
                "signed upload timed out",
                qa["issues"][0]["description"],
            )

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
