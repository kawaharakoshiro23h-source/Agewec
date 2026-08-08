from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agewec_v2.paths import PACKAGE_ROOT, RuntimePaths
from agewec_v2.phase_contracts import (
    effective_target_cut_id,
    missing_contract_phases,
    preserves_existing_artifact,
)


class RuntimePathsTest(unittest.TestCase):
    def test_default_layout_uses_project_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = (Path(directory) / "project").resolve()
            workflow = (project / "workflow_v2").resolve()
            layout = RuntimePaths.from_config(
                {}, project_root=project, workflow_root=workflow
            )

            self.assertEqual(layout.runtime_root, project / "runtime")
            self.assertEqual(layout.work_root, project / "runtime")
            self.assertEqual(layout.runs_root, project / "runtime" / "runs")
            self.assertEqual(
                layout.submissions_root,
                project / "runtime" / "submissions",
            )
            self.assertEqual(layout.assets_root, project / "assets_dl")
            self.assertEqual(layout.asset_catalog, project / "asset_catalog.json")
            self.assertEqual(
                layout.checkpoint_db,
                project / "runtime" / "checkpoints" / "checkpoints.sqlite",
            )
            self.assertEqual(layout.prompt_root, PACKAGE_ROOT / "prompts")

    def test_configured_paths_resolve_against_their_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = (root / "project").resolve()
            workflow = (project / "workflow_v2").resolve()
            external_runs = (root / "runs").resolve()
            layout = RuntimePaths.from_config(
                {
                    "paths": {
                        "work_dir": "var",
                        "runs_dir": external_runs,
                        "submissions_dir": "deliverables/generated",
                        "assets_dir": "runtime/assets",
                        "asset_catalog": "runtime/catalog.json",
                        "checkpoint_db": "var/state.sqlite",
                    }
                },
                project_root=project,
                workflow_root=workflow,
            )

            self.assertEqual(layout.work_root, workflow / "var")
            self.assertEqual(layout.runs_root, external_runs)
            self.assertEqual(
                layout.submissions_root, workflow / "deliverables/generated"
            )
            self.assertEqual(layout.assets_root, project / "runtime/assets")
            self.assertEqual(
                layout.asset_catalog, project / "runtime/catalog.json"
            )
            self.assertEqual(layout.checkpoint_db, workflow / "var/state.sqlite")
            self.assertEqual(
                layout.cut_path("run-1", 3, "attempt.mp4"),
                external_runs / "run-1" / "cuts/cut_03/attempt.mp4",
            )

    def test_runtime_layout_resolves_execution_paths_from_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = (Path(directory) / "project").resolve()
            workflow = (project / "workflow_v2").resolve()
            layout = RuntimePaths.from_config(
                {
                    "paths": {
                        "runtime_dir": "var/runtime",
                        "runs_dir": "executions",
                        "submissions_dir": "packages",
                        "checkpoint_db": "state/checkpoints.sqlite",
                        "provenance_file": "shared/provenance.json",
                    }
                },
                project_root=project,
                workflow_root=workflow,
            )

            runtime = project / "var/runtime"
            self.assertEqual(layout.runtime_root, runtime)
            self.assertEqual(layout.work_root, runtime)
            self.assertEqual(layout.runs_root, runtime / "executions")
            self.assertEqual(layout.submissions_root, runtime / "packages")
            self.assertEqual(
                layout.checkpoint_db,
                runtime / "state/checkpoints.sqlite",
            )
            self.assertEqual(
                layout.provenance_file,
                runtime / "shared/provenance.json",
            )

    def test_explicit_legacy_paths_keep_workflow_relative_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = (Path(directory) / "project").resolve()
            workflow = (project / "workflow_v2").resolve()
            layout = RuntimePaths.from_config(
                {
                    "paths": {
                        "work_dir": "work",
                        "runs_dir": "runs",
                        "submissions_dir": "submissions",
                        "checkpoint_db": "work/checkpoints.sqlite",
                    }
                },
                project_root=project,
                workflow_root=workflow,
            )

            self.assertEqual(layout.work_root, workflow / "work")
            self.assertEqual(layout.runs_root, workflow / "work/runs")
            self.assertEqual(layout.submissions_root, workflow / "submissions")
            self.assertEqual(
                layout.checkpoint_db,
                workflow / "work/checkpoints.sqlite",
            )


class PhaseContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.shots = [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_every_runtime_phase_has_a_declared_contract(self) -> None:
        self.assertEqual(missing_contract_phases(), set())

    def test_target_is_ignored_without_a_complete_request_base(self) -> None:
        self.assertIsNone(effective_target_cut_id(3, self.shots, {}))
        self.assertIsNone(
            effective_target_cut_id(
                3,
                self.shots,
                {"1": {"cut_id": 1}, "2": {"cut_id": 2}},
            )
        )

    def test_target_is_retained_with_a_complete_request_base(self) -> None:
        existing = {
            str(cut_id): {"cut_id": cut_id}
            for cut_id in (1, 2, 3)
        }
        self.assertEqual(
            effective_target_cut_id(3, self.shots, existing),
            3,
        )

    def test_only_direct_regeneration_preserves_existing_artifact(self) -> None:
        self.assertTrue(preserves_existing_artifact("image_video_production"))
        for route in ("director", "asset_curator", "support_video_creator"):
            self.assertFalse(preserves_existing_artifact(route))


if __name__ == "__main__":
    unittest.main()
