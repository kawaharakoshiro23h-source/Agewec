"""Sequence QAと提出Package作成の安全性回帰テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agewec_v2.pipeline_runtime import (
    provenance_package,
    sequence_visual_qa,
)


def _base_state(directory: str, *, run_id: str = "run-safety") -> dict:
    return {
        "run_id": run_id,
        "project": {"target_duration_seconds": 5.0},
        "config": {
            "paths": {
                "work_dir": str(Path(directory) / "work"),
                "submissions_dir": str(Path(directory) / "submissions"),
            }
        },
        "phase_results": {
            "writer_storyboard": {
                "data": {
                    "cuts": [{"id": 1, "seconds": 5.0}],
                }
            }
        },
        "approved_cut_ids": [1],
        "production_artifacts": {},
        "attempts": {},
        "feedback": {},
        "review_context": {},
        "reviews": [],
        "events": [],
        "artifacts": [],
        "phase_timings": {},
    }


class SequenceVisualQASafetyTest(unittest.TestCase):
    def test_approved_cut_without_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _base_state(directory)

            update = sequence_visual_qa(state)

        result = update["phase_results"]["visual_qa"]
        codes = {issue["code"] for issue in result["data"]["issues"]}
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["data"]["verdict"], "revise")
        self.assertIn("MISSING_PRODUCTION_ARTIFACTS", codes)
        self.assertEqual(result["data"]["affected_cut_ids"], [1])

    def test_empty_missing_and_directory_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "empty": "",
                "missing": str(root / "missing.mp4"),
                "directory": str(root),
            }
            for label, path in cases.items():
                with self.subTest(label=label):
                    state = _base_state(directory, run_id=f"run-{label}")
                    state["production_artifacts"] = {
                        "1": {"path": path, "kind": "video"}
                    }

                    update = sequence_visual_qa(state)

                    result = update["phase_results"]["visual_qa"]
                    codes = {
                        issue["code"] for issue in result["data"]["issues"]
                    }
                    self.assertEqual(result["status"], "error")
                    self.assertIn("MISSING_PRODUCTION_FILES", codes)

    def test_existing_artifact_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "cut_01.mp4"
            video.write_bytes(b"video")
            state = _base_state(directory)
            state["production_artifacts"] = {
                "1": {"path": str(video), "kind": "video"}
            }

            update = sequence_visual_qa(state)

        result = update["phase_results"]["visual_qa"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["verdict"], "pass")
        self.assertEqual(result["data"]["issues"], [])


class ProvenanceSafetyTest(unittest.TestCase):
    def test_invalid_final_output_returns_error_without_creating_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "none": None,
                "empty": "",
                "missing": str(root / "missing.mp4"),
                "directory": str(root),
            }
            for label, final_output in cases.items():
                with self.subTest(label=label):
                    run_id = f"run-{label}"
                    state = _base_state(directory, run_id=run_id)
                    state["final_output"] = final_output

                    update = provenance_package(state)

                    result = update["phase_results"]["provenance"]
                    package = root / "submissions" / run_id
                    self.assertEqual(result["status"], "error")
                    self.assertIn(
                        "実在する動画ファイルではない",
                        result["blocking_issues"][0],
                    )
                    self.assertFalse(package.exists())


if __name__ == "__main__":
    unittest.main()
