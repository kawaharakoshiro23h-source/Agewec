from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agewec_v2.test_pipeline_1cut import (
    _result_checks,
    isolate_single_cut,
)


class OneCutPipelineSmokeTest(unittest.TestCase):
    def _state(self, image_path: str) -> dict:
        asset = {
            "asset_id": "asset-123",
            "title": "北九州",
            "local_path": image_path,
            "eligible_cut_ids": [1],
            "selection_reason": "このカットに適合",
        }
        return {
            "run_id": "one-cut-test",
            "config": {
                "qa": {
                    "duration_tolerance_seconds": 0.25,
                    "representative_frame_count": 3,
                }
            },
            "phase_results": {
                "asset_curator": {
                    "data": {
                        "asset_assignments": [
                            {
                                "cut_id": 1,
                                "primary": asset,
                                "alternatives": [],
                            }
                        ]
                    }
                },
                "director": {
                    "data": {
                        "shots": [
                            {
                                "id": 1,
                                "seconds": 6,
                                "asset": asset,
                                "positive_prompt": "slow cinematic push in",
                                "negative_prompt": "distortion",
                                "camera_motion": "slow push in",
                                "rationale": "導入に適合",
                            },
                            {
                                "id": 2,
                                "seconds": 6,
                                "asset": {"asset_id": "asset-999"},
                                "positive_prompt": "other",
                            },
                        ]
                    }
                },
            },
            "production_requests": {},
            "production_artifacts": {},
            "cut_qa_results": {},
        }

    def test_isolation_preserves_asset_and_prompt_and_overrides_only_duration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.jpg"
            image.write_bytes(b"image")
            update = isolate_single_cut(
                self._state(str(image)),
                cut_id=1,
                seconds=2.0,
            )

        shots = update["phase_results"]["director"]["data"]["shots"]
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["asset"]["asset_id"], "asset-123")
        self.assertEqual(
            shots[0]["positive_prompt"],
            "slow cinematic push in",
        )
        self.assertEqual(shots[0]["seconds"], 2.0)
        self.assertEqual(
            update["smoke_test"]["original_storyboard_seconds"],
            6.0,
        )
        self.assertTrue(
            update["smoke_test"]["duration_overridden_for_smoke_test"]
        )

    def test_isolation_rejects_shortlist_candidate_for_another_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.jpg"
            image.write_bytes(b"image")
            state = self._state(str(image))
            selected = state["phase_results"]["director"]["data"][
                "shots"
            ][0]["asset"]
            selected["eligible_cut_ids"] = [2]
            with self.assertRaisesRegex(
                RuntimeError,
                "shortlist eligibility",
            ):
                isolate_single_cut(
                    state,
                    cut_id=1,
                    seconds=2.0,
                )

    def test_result_checks_include_exact_request_propagation_and_media(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "cut.mp4"
            video.write_bytes(b"video")
            frames = []
            for index in range(3):
                frame = root / f"frame_{index}.jpg"
                frame.write_bytes(b"frame")
                frames.append(str(frame))
            state = self._state(str(root / "input.jpg"))
            state["smoke_test"] = {
                "director": {
                    "image_path": str(root / "input.jpg"),
                    "positive_prompt": "positive",
                    "negative_prompt": "negative",
                }
            }
            state["production_requests"] = {
                "1": {
                    "image_path": str(root / "input.jpg"),
                    "positive_prompt": "positive",
                    "negative_prompt": "negative",
                    "actual_seconds": 2.0417,
                    "width": 576,
                    "height": 384,
                    "fps": 24,
                    "frames": 49,
                }
            }
            state["production_artifacts"] = {
                "1": {"path": str(video)}
            }
            state["cut_qa_results"] = {
                "1": {
                    "verdict": "pass",
                    "issue_class": "pass",
                    "technical": {
                        "duration_seconds": 2.042,
                        "width": 576,
                        "height": 384,
                        "fps": 24,
                        "frame_count": 49,
                    },
                    "representative_frames": frames,
                }
            }
            checks = _result_checks(state, cut_id=1)
            self.assertTrue(all(checks.values()), checks)


if __name__ == "__main__":
    unittest.main()
