"""Stage 3 module boundaries and compatibility exports."""
from __future__ import annotations

import unittest
from pathlib import Path

from agewec_v2 import nodes_runtime, pipeline_runtime
from agewec_v2.phases import (
    cut_qa,
    post_production,
    production,
    provenance,
    sequence_qa,
    support_video,
)


class PhaseModuleBoundaryTest(unittest.TestCase):
    def test_graph_entrypoints_use_split_modules_directly(self) -> None:
        expected = {
            "support_video_creator": support_video.support_video_creator,
            "image_video_production": production.image_video_production,
            "cut_visual_qa": cut_qa.cut_visual_qa,
            "commit_cut_qa": cut_qa.commit_cut_qa,
            "visual_qa": sequence_qa.sequence_visual_qa,
            "post_production": post_production.post_production,
            "review_board": post_production.review_board,
            "provenance": provenance.provenance_package,
        }
        for name, implementation in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(nodes_runtime, name), implementation)

    def test_legacy_pipeline_runtime_exports_the_same_functions(self) -> None:
        expected = {
            "support_video_creator": support_video.support_video_creator,
            "image_video_production": production.image_video_production,
            "cut_visual_qa": cut_qa.cut_visual_qa,
            "commit_cut_qa": cut_qa.commit_cut_qa,
            "sequence_visual_qa": sequence_qa.sequence_visual_qa,
            "post_production": post_production.post_production,
            "review_board": post_production.review_board,
            "provenance_package": provenance.provenance_package,
        }
        for name, implementation in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(pipeline_runtime, name), implementation)

    def test_phase_implementations_do_not_import_compatibility_module(self) -> None:
        phases = Path(__file__).resolve().parents[1] / "agewec_v2/phases"
        for source in phases.glob("*.py"):
            with self.subTest(source=source.name):
                self.assertNotIn(
                    "pipeline_runtime",
                    source.read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
