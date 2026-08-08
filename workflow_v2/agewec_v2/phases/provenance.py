"""Phase 10 submission package and provenance materialization."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..fallbacks import common as deterministic
from .. import timing
from ..media_tools import downscale_image
from ..paths import runtime_paths
from ..state import WorkflowState

from .common import _json_write, _result_data
from .reporting import (
    _decision_log, _process_html, _process_markdown, _sha256,
)

def _sha256_of(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _copy_cut_sources(state: WorkflowState, package: Path) -> list[dict]:
    """使用元画像（縮小）とカット別動画をPackageへコピーし、索引を返す。

    元画像は最大20MB規模のためPackage肥大化を避けて長辺1280pxへ縮小する。
    追跡性は asset_id / source_url / sha256（原本のもの）で担保する。
    """
    shots = {
        int(s.get("id", s.get("cut_id", 0))): s
        for s in state.get("phase_results", {})
        .get("director", {})
        .get("data", {})
        .get("shots", [])
    }
    artifacts = state.get("production_artifacts", {})
    src_dir = package / "artifacts" / "sources"
    clip_dir = package / "artifacts" / "cuts"
    index: list[dict[str, Any]] = []

    for cut_id in sorted(set(shots) | {int(k) for k in artifacts}):
        shot = shots.get(cut_id, {})
        asset = shot.get("asset") or {}
        entry: dict[str, Any] = {
            "cut_id": cut_id,
            # 出典の有無は generation_mode で決まる。text_to_video のカットは
            # 元になった写真が存在しないため、その旨を明記して記録する。
            "generation_mode": str(
                shot.get("generation_mode") or "image_to_video"
            ),
            "model": str(
                shot.get("model")
                or (
                    state.get("production_requests", {}).get(str(cut_id))
                    or {}
                ).get("model")
                or state.get("config", {}).get("production", {}).get("model")
                or ""
            ) or None,
            "asset_id": asset.get("asset_id"),
            "title": asset.get("title"),
            "source_url": asset.get("source_url"),
            "detail_url": asset.get("detail_url"),
            "original_local_path": asset.get("local_path"),
            "original_sha256": asset.get("sha256"),
            "selection_reason": asset.get("selection_reason"),
        }
        # 空文字は Path(".") になり exists() を通過してしまう（P0-3と同じ罠）。
        # text_to_video のカットは local_path を持たないため必ずここを通る。
        original = Path(str(asset.get("local_path") or ""))
        if str(asset.get("local_path") or "") and original.is_file():
            if not entry["original_sha256"]:
                entry["original_sha256"] = _sha256_of(original)
            src_dir.mkdir(parents=True, exist_ok=True)
            preview = src_dir / f"cut_{cut_id:02d}_source{original.suffix}"
            try:
                downscale_image(str(original), str(preview), max_edge=1280)
            except Exception:  # noqa: BLE001 - 縮小失敗時は原本をコピー
                shutil.copy2(original, preview)
            entry["preview_path"] = str(preview.relative_to(package))

        raw_clip = str((artifacts.get(str(cut_id)) or {}).get("path") or "")
        clip = Path(raw_clip)
        if raw_clip and clip.is_file():
            clip_dir.mkdir(parents=True, exist_ok=True)
            destination = clip_dir / f"cut_{cut_id:02d}{clip.suffix}"
            shutil.copy2(clip, destination)
            entry["clip_path"] = str(destination.relative_to(package))
            entry["clip_sha256"] = _sha256_of(destination)
        index.append(entry)

    _json_write(package / "cut_sources.json", {"cuts": index})
    return index


def provenance_package(state: WorkflowState) -> dict[str, Any]:
    phase = "provenance"
    run_id = str(state.get("run_id") or f"run-{int(time.time())}")
    raw_source_video = str(state.get("final_output") or "").strip()
    source_video = Path(raw_source_video)
    if not raw_source_video or not source_video.is_file():
        return deterministic._complete(
            state,
            phase,
            summary="最終動画がないため提出Packageを作成不可",
            data={
                "final_output": raw_source_video or None,
                "is_file": False,
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                "final_outputが実在する動画ファイルではない"
            ],
        )
    package = runtime_paths(state.get("config", {})).submissions_root / run_id
    package.mkdir(parents=True, exist_ok=True)
    final_video = package / "final_video.mp4"
    if source_video.resolve() != final_video.resolve():
        shutil.copy2(source_video, final_video)

    phase_results = deterministic._sanitized(
        state.get("phase_results", {})
    )
    provenance = {
        "run_id": run_id,
        "project": state.get("project", {}),
        "config": deterministic._sanitized(state.get("config", {})),
        "phase_results": phase_results,
        "reviews": state.get("reviews", []),
        "events": state.get("events", []),
        "artifacts": state.get("artifacts", []),
        "phase_timings": state.get("phase_timings", {}),
        "timing_summary": timing.summarize(state),
    }
    _json_write(package / "provenance.json", provenance)
    _json_write(package / "timing_report.json", timing.summarize(state))

    # run 直下にも実行履歴を残す（提出Packageとは別に、作業用の記録として）
    run_dir = deterministic._work_path(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    _json_write(run_dir / "state.json", provenance)
    events_file = run_dir / "events.jsonl"
    events_file.write_text(
        "\n".join(
            json.dumps(event, ensure_ascii=False)
            for event in state.get("events", [])
        ),
        encoding="utf-8",
    )
    _json_write(
        package / "storyboard.json",
        _result_data(state, "writer_storyboard"),
    )
    _json_write(
        package / "direction_plan.json",
        _result_data(state, "director"),
    )
    _json_write(
        package / "review_summary.json",
        {
            "review_board": _result_data(state, "review_board"),
            "reviews": state.get("reviews", []),
        },
    )
    technical = _result_data(state, "post_production").get(
        "technical_qa",
        {},
    )
    _json_write(package / "technical_report.json", technical)
    decisions = _decision_log(state)
    (package / "decision_log.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in decisions
        ),
        encoding="utf-8",
    )
    markdown = _process_markdown(state, final_video.name)
    (package / "process_report.md").write_text(
        markdown,
        encoding="utf-8",
    )
    (package / "process_report.html").write_text(
        _process_html(state, final_video.name),
        encoding="utf-8",
    )

    qa_dir = package / "artifacts" / "qa"
    copied_qa: list[str] = []
    for result in state.get("cut_qa_results", {}).values():
        cut_id = int(result.get("cut_id", 0))
        for frame in result.get("representative_frames", []):
            source = Path(frame)
            if not source.exists():
                continue
            destination = qa_dir / f"cut_{cut_id:02d}_{source.name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_qa.append(str(destination.relative_to(package)))

    # 使用した元画像（レビュー用に縮小）とカット別の動画をPackageへ含める。
    # 元画像の asset_id / source_url / sha256 は原本の証跡として必ず記録する。
    source_index = _copy_cut_sources(state, package)


    required = [
        final_video,
        package / "provenance.json",
        package / "technical_report.json",
        package / "process_report.html",
        package / "process_report.md",
        package / "decision_log.jsonl",
        package / "storyboard.json",
        package / "direction_plan.json",
        package / "review_summary.json",
        package / "cut_sources.json",
    ]
    manifest_files = []
    for path in required:
        manifest_files.append(
            {
                "kind": path.stem,
                "path": str(path.relative_to(package)),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "run_id": run_id,
        "status": "ready_for_submission",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest_files,
        "qa_artifacts": copied_qa,
    }
    _json_write(package / "manifest.json", manifest)
    required.append(package / "manifest.json")
    missing = [str(path) for path in required if not path.exists()]
    status = "success" if not missing else "error"
    artifacts = [
        {
            "phase": phase,
            "kind": path.stem,
            "path": str(path),
        }
        for path in required
    ]
    update = deterministic._complete(
        state,
        phase,
        summary=f"提出Packageを生成: {package}",
        data={
            "package_dir": str(package),
            "final_video": str(final_video),
            "provenance": str(package / "provenance.json"),
            "process_report": str(package / "process_report.html"),
            "manifest": str(package / "manifest.json"),
            "status": (
                "ready_for_submission"
                if not missing
                else "error"
            ),
        },
        artifacts=artifacts,
        status=status,
        confidence=1.0 if not missing else 0.0,
        blocking_issues=missing,
    )
    update.update(
        {
            "final_output": str(final_video),
            "provenance_output": str(package / "provenance.json"),
            "process_report_output": str(
                package / "process_report.html"
            ),
            "submission_manifest": str(package / "manifest.json"),
        }
    )
    return update
