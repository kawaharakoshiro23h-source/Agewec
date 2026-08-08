"""Canonical project and runtime path resolution.

All relative paths in workflow configuration are resolved here.  Keeping this
logic out of phase implementations makes the later ``runtime/`` and ``src/``
migrations mechanical instead of behavioural changes.

New executions use ``<project>/runtime``.  Configurations that explicitly use
the pre-refactor path keys without ``runtime_dir`` keep their legacy
workflow-relative meaning, so persisted runs and focused tests remain readable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent


def _detected_roots() -> tuple[Path, Path]:
    """Return ``(workflow_root, project_root)`` for source and installed use."""
    project_override = os.environ.get("AGEWEC_PROJECT_ROOT")
    workflow_override = os.environ.get("AGEWEC_WORKFLOW_ROOT")
    if project_override:
        project_root = Path(project_override).expanduser().resolve()
        workflow_root = (
            Path(workflow_override).expanduser().resolve()
            if workflow_override
            else project_root / "workflow_v2"
        )
        return workflow_root, project_root

    package_parent = PACKAGE_ROOT.parent
    if package_parent.name == "workflow_v2":
        return package_parent, package_parent.parent
    if package_parent.name == "src":
        project_root = package_parent.parent
        return project_root, project_root

    # Installed wheel: use the caller's project directory.  Explicit config
    # paths or AGEWEC_*_ROOT can override this when invoked elsewhere.
    project_root = Path.cwd().resolve()
    legacy_workflow = project_root / "workflow_v2"
    workflow_root = (
        legacy_workflow if legacy_workflow.is_dir() else project_root
    )
    return workflow_root, project_root


WORKFLOW_ROOT, PROJECT_ROOT = _detected_roots()


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved paths used by workflow execution and generated artifacts."""

    project_root: Path
    workflow_root: Path
    runtime_root: Path
    work_root: Path
    runs_root: Path
    submissions_root: Path
    assets_root: Path
    asset_catalog: Path
    checkpoint_db: Path
    provenance_file: Path
    prompt_root: Path

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None = None,
        *,
        project_root: Path | None = None,
        workflow_root: Path | None = None,
    ) -> "RuntimePaths":
        config = config or {}
        values = config.get("paths", {}) or {}
        project = (project_root or PROJECT_ROOT).expanduser().resolve()
        workflow = (workflow_root or WORKFLOW_ROOT).expanduser().resolve()
        runtime = _resolve(project, values.get("runtime_dir", "runtime"))

        legacy_keys = {
            "work_dir",
            "runs_dir",
            "submissions_dir",
            "checkpoint_db",
            "provenance_file",
        }
        uses_runtime_layout = (
            "runtime_dir" in values
            or not any(key in values for key in legacy_keys)
        )
        if uses_runtime_layout:
            work = _resolve(runtime, values.get("work_dir", "."))
            runs = _resolve(runtime, values.get("runs_dir", "runs"))
            submissions = _resolve(
                runtime,
                values.get("submissions_dir", "submissions"),
            )
            checkpoint = _resolve(
                runtime,
                values.get(
                    "checkpoint_db",
                    "checkpoints/checkpoints.sqlite",
                ),
            )
            provenance = _resolve(
                runtime,
                values.get("provenance_file", "provenance.json"),
            )
        else:
            # Compatibility for stored configs and callers that still provide
            # the workflow_v2-relative Stage 0 layout.
            work = _resolve(workflow, values.get("work_dir", "work"))
            runs = _resolve(work, values.get("runs_dir", "runs"))
            submissions = _resolve(
                workflow,
                values.get("submissions_dir", "submissions"),
            )
            checkpoint = _resolve(
                workflow,
                values.get("checkpoint_db", "work/checkpoints.sqlite"),
            )
            provenance = _resolve(
                workflow,
                values.get("provenance_file", "work/provenance.json"),
            )
        assets = _resolve(
            project,
            values.get("assets_dir", "assets_dl"),
        )
        catalog = _resolve(
            project,
            values.get("asset_catalog", "asset_catalog.json"),
        )
        prompt = _resolve(
            PACKAGE_ROOT,
            values.get("prompt_dir", "prompts"),
        )
        return cls(
            project_root=project,
            workflow_root=workflow,
            runtime_root=runtime,
            work_root=work,
            runs_root=runs,
            submissions_root=submissions,
            assets_root=assets,
            asset_catalog=catalog,
            checkpoint_db=checkpoint,
            provenance_file=provenance,
            prompt_root=prompt,
        )

    def run_dir(self, run_id: str | None) -> Path:
        return self.runs_root / run_id if run_id else self.work_root

    def work_path(self, run_id: str | None, *parts: str) -> Path:
        path = self.run_dir(run_id)
        for part in parts:
            path /= part
        return path

    def cut_path(
        self,
        run_id: str | None,
        cut_id: int,
        *parts: str,
    ) -> Path:
        return self.work_path(
            run_id,
            "cuts",
            f"cut_{int(cut_id):02d}",
            *parts,
        )

    def resolve_workflow(self, value: str | Path) -> Path:
        return _resolve(self.workflow_root, value)

    def resolve_runtime(self, value: str | Path) -> Path:
        return _resolve(self.runtime_root, value)

    def resolve_project(self, value: str | Path) -> Path:
        return _resolve(self.project_root, value)


def runtime_paths(config: dict[str, Any] | None = None) -> RuntimePaths:
    """Convenience constructor used by runtime code."""
    return RuntimePaths.from_config(config)


def legacy_checkpoint_db(workflow_root: Path | None = None) -> Path:
    """Return the pre-Stage-5 checkpoint location for resume compatibility."""
    workflow = (workflow_root or WORKFLOW_ROOT).expanduser().resolve()
    return workflow / "work" / "checkpoints.sqlite"
