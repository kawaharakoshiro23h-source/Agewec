"""Stage 3 module boundaries and compatibility exports."""
from __future__ import annotations

import unittest

from agewec_v2 import nodes, nodes_llm, nodes_runtime, pipeline_runtime
from agewec_v2.paths import PACKAGE_ROOT
from agewec_v2.fallbacks import (
    assets as fallback_assets,
    director as fallback_director,
    planning as fallback_planning,
)
from agewec_v2.phases import (
    cut_qa,
    post_production,
    production,
    provenance,
    sequence_qa,
    support_video,
)
from agewec_v2.roles import (
    assets as asset_role,
    director as director_role,
    project as project_roles,
    storyboard as storyboard_role,
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
        phases = PACKAGE_ROOT / "phases"
        for source in phases.glob("*.py"):
            with self.subTest(source=source.name):
                self.assertNotIn(
                    "pipeline_runtime",
                    source.read_text(encoding="utf-8"),
                )

    def test_graph_entrypoints_use_split_role_modules_directly(self) -> None:
        expected = {
            "executive_producer": project_roles.executive_producer,
            "creative_director": project_roles.creative_director,
            "writer_storyboard": storyboard_role.writer_storyboard,
            "asset_curator": asset_role.asset_curator,
            "director": director_role.director,
        }
        for name, implementation in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(nodes_runtime, name), implementation)

    def test_legacy_role_exports_reference_split_implementations(self) -> None:
        expected = {
            "executive_producer": project_roles.executive_producer,
            "creative_director": project_roles.creative_director,
            "writer_storyboard": storyboard_role.writer_storyboard,
            "asset_curator": asset_role.asset_curator,
            "director": director_role.director,
        }
        for name, implementation in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(nodes_llm, name), implementation)

    def test_legacy_fallback_exports_reference_split_implementations(self) -> None:
        expected = {
            "executive_producer": fallback_planning.executive_producer,
            "creative_director": fallback_planning.creative_director,
            "writer_storyboard": fallback_planning.writer_storyboard,
            "asset_curator": fallback_assets.asset_curator,
            "director": fallback_director.director,
        }
        for name, implementation in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(nodes, name), implementation)

    def test_split_roles_do_not_import_legacy_facades(self) -> None:
        package = PACKAGE_ROOT
        for directory in (package / "roles", package / "fallbacks"):
            for source in directory.glob("*.py"):
                text = source.read_text(encoding="utf-8")
                with self.subTest(source=str(source.relative_to(package))):
                    self.assertNotIn("from .. import nodes ", text)
                    self.assertNotIn("from .. import nodes_llm", text)


if __name__ == "__main__":
    unittest.main()
