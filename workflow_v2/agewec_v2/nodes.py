"""Role nodes for the isolated AGEWEC v2 workflow.

The current role outputs are deterministic scaffolding. Each function has a stable
structured contract so an LLM/VLM/tool implementation can replace its internals
without changing graph routing or Review Gates.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .backends import ComfyClient, ComfyGenerationRequest
from .state import WorkflowState


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WORKFLOW_ROOT.parent

AWARD_GENRES = {
    "夜景賞": "イルミネーション・夜景",
    "観光賞": "観光スポット",
    "環境賞": "公園",
    "DX賞": None,
}


def _work_path(state: WorkflowState, *parts: str) -> Path:
    relative = state.get("config", {}).get("paths", {}).get("work_dir", "work")
    path = WORKFLOW_ROOT / relative
    for part in parts:
        path /= part
    return path


def _phase_feedback(state: WorkflowState, phase: str) -> str:
    return state.get("feedback", {}).get(phase, "")


def _complete(
    state: WorkflowState,
    phase: str,
    *,
    summary: str,
    data: dict[str, Any],
    artifacts: list[dict[str, Any]] | None = None,
    status: str = "success",
    confidence: float | None = None,
    blocking_issues: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    attempts = dict(state.get("attempts", {}))
    attempts[phase] = attempts.get(phase, 0) + 1
    result = {
        "phase": phase,
        "status": status,
        "summary": summary,
        "data": data,
        "artifacts": artifacts or [],
        "confidence": confidence,
        "blocking_issues": blocking_issues or [],
        "warnings": warnings or [],
        "attempt": attempts[phase],
        "feedback_applied": _phase_feedback(state, phase),
    }
    phase_results = dict(state.get("phase_results", {}))
    phase_results[phase] = result
    all_artifacts = list(state.get("artifacts", []))
    all_artifacts.extend(artifacts or [])
    events = list(state.get("events", []))
    events.append(
        {
            "t": round(time.time(), 3),
            "type": "phase_completed",
            "phase": phase,
            "status": status,
            "attempt": attempts[phase],
            "summary": summary,
        }
    )
    return {
        "current_phase": phase,
        "phase_results": phase_results,
        "attempts": attempts,
        "events": events,
        "artifacts": all_artifacts,
    }


def executive_producer(state: WorkflowState) -> dict[str, Any]:
    phase = "executive_producer"
    project = state.get("project", {})
    duration = int(project.get("target_duration_seconds", 30))
    brief = {
        "objective": project.get("theme", "北九州の魅力を世界へ"),
        "target_award": project.get("target_award", "夜景賞"),
        "audience": "北九州をまだ訪れたことのない国内外の旅行者",
        "deliverable": f"{duration}秒の観光プロモーション動画",
        "constraints": [
            "素材の出典と利用条件を記録する",
            "ローカル生成を基本とし、バックエンドを交換可能にする",
            "人間が重要判断を承認できる",
        ],
        "success_criteria": [
            "北九州固有の魅力が伝わる",
            "映像とナレーションの主張が一致する",
            "提出資料から生成過程を追跡できる",
        ],
    }
    return _complete(
        state,
        phase,
        summary=f"{brief['deliverable']}の制作方針を定義",
        data=brief,
        confidence=0.9,
    )


def creative_director(state: WorkflowState) -> dict[str, Any]:
    phase = "creative_director"
    brief = state["phase_results"]["executive_producer"]["data"]
    concept = {
        "title": "光がつなぐ、北九州",
        "logline": "産業の光、街の光、人の営みを一続きの旅として描く。",
        "tone": ["cinematic", "authentic", "quietly futuristic"],
        "visual_language": {
            "palette": ["deep blue", "warm amber", "steel gray"],
            "camera": "緩やかなプッシュイン、パン、俯瞰",
            "continuity_rule": "夜へ向かう時間軸と光のモチーフを維持する",
        },
        "audio_direction": "静かな導入から希望を感じる広がりへ",
        "success_criteria": brief["success_criteria"],
    }
    return _complete(
        state,
        phase,
        summary=f"コンセプト「{concept['title']}」を策定",
        data=concept,
        confidence=0.88,
    )


def writer_storyboard(state: WorkflowState) -> dict[str, Any]:
    phase = "writer_storyboard"
    duration = int(state.get("project", {}).get("target_duration_seconds", 30))
    base_cuts = [
        ("導入", "夕暮れから夜へ移る北九州の全景", "光が目覚める街、北九州。", "still"),
        ("夜景", "皿倉山から広がる街の光", "百万の光が、ひとつの物語になる。", "video"),
        ("産業", "海と工場群の光が水面に反射する", "ものづくりの記憶は、未来を照らす。", "video"),
        ("歴史", "歴史的建築と現代の街並み", "受け継いだ時間が、新しい景色をつくる。", "still"),
        ("人と街", "街を歩く人々と交通の光跡", "ここには、暮らしの温度がある。", "video"),
        ("締め", "夜景から北九州のタイトルへ", "光の先へ。北九州で会いましょう。", "still"),
    ]
    seconds = max(2.0, duration / len(base_cuts))
    cuts = []
    for index, (name, scene, narration, strategy) in enumerate(base_cuts, start=1):
        cuts.append(
            {
                "id": index,
                "name": name,
                "scene": scene,
                "narration": narration,
                "seconds": round(seconds, 2),
                "media_strategy": strategy,
            }
        )
    storyboard = {
        "total_seconds": round(sum(c["seconds"] for c in cuts), 2),
        "cuts": cuts,
    }
    return _complete(
        state,
        phase,
        summary=f"{len(cuts)}カット、約{storyboard['total_seconds']}秒の絵コンテを作成",
        data=storyboard,
        confidence=0.86,
    )


def _load_catalog() -> dict[str, Any]:
    path = PROJECT_ROOT / "asset_catalog.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _local_asset_path(image_url: str) -> str | None:
    name = unquote(Path(urlparse(image_url).path).name)
    candidate = PROJECT_ROOT / "assets_dl" / name
    return str(candidate) if name and candidate.exists() else None


def asset_curator(state: WorkflowState) -> dict[str, Any]:
    phase = "asset_curator"
    award = state.get("project", {}).get("target_award", "夜景賞")
    genre = AWARD_GENRES.get(award)
    catalog = _load_catalog()
    candidates = catalog.get("photos", [])
    matches = (
        [p for p in candidates if genre in p.get("genres", [])]
        if genre
        else candidates
    )
    selected = []
    for photo in matches[:6]:
        selected.append(
            {
                "title": photo.get("title", ""),
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": photo.get("genres", []),
                "areas": photo.get("areas", []),
                "local_path": _local_asset_path(photo.get("image_url", "")),
                "rights_status": "review_required",
                "rights_note": "公式配布元の最新利用条件を提出前に確認する",
            }
        )
    warnings = []
    if not selected:
        warnings.append("賞テーマに一致する公式素材候補が見つからない")
    if selected and not any(item["local_path"] for item in selected):
        warnings.append("候補画像がローカルに未取得")
    manifest = {
        "catalog_source": catalog.get("source"),
        "target_genre": genre,
        "selected_assets": selected,
        "rights_check_required": True,
    }
    return _complete(
        state,
        phase,
        summary=f"公式素材候補を{len(selected)}件選定し、権利確認状態を記録",
        data=manifest,
        confidence=0.82 if selected else 0.55,
        blocking_issues=[] if selected else ["利用可能な素材候補がない"],
        warnings=warnings,
    )


def director(state: WorkflowState) -> dict[str, Any]:
    phase = "director"
    cuts = state["phase_results"]["writer_storyboard"]["data"]["cuts"]
    assets = state["phase_results"]["asset_curator"]["data"]["selected_assets"]
    production = state.get("config", {}).get("production", {})
    profile_name = production.get("profile", "draft")
    profile = production.get("profiles", {}).get(profile_name, {})
    shot_plan = []
    for index, cut in enumerate(cuts):
        asset = assets[index % len(assets)] if assets else {}
        motion = (
            "slow cinematic push-in, subtle parallax, preserve the original "
            "Kitakyushu cityscape, natural lights, stable architecture"
        )
        shot_plan.append(
            {
                **cut,
                "asset": asset,
                "positive_prompt": (
                    f"{cut['scene']}. {motion}. Deep blue and warm amber color "
                    "palette, realistic documentary cinematography."
                ),
                "negative_prompt": "",
                "generation_profile": profile,
            }
        )
    plan = {
        "profile_name": profile_name,
        "shots": shot_plan,
        "continuity_checks": [
            "deep blueとwarm amberを維持",
            "建築・地形を過度に変形させない",
            "カメラ移動は緩やかにする",
        ],
    }
    return _complete(
        state,
        phase,
        summary=f"{len(shot_plan)}カットの素材・演出・生成設定を確定",
        data=plan,
        confidence=0.85,
        blocking_issues=[] if assets else ["演出に割り当てる素材がない"],
    )


def _mock_production(
    state: WorkflowState,
    shots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    output_dir = _work_path(state, "production")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for shot in shots:
        path = output_dir / f"shot_{shot['id']:02d}_request.json"
        path.write_text(
            json.dumps(shot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "phase": "image_video_production",
                "cut_id": shot["id"],
                "kind": "generation_request",
                "path": str(path),
                "backend": "mock",
            }
        )
    return artifacts, []


def _comfy_production(
    state: WorkflowState,
    shots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    config = state.get("config", {})
    comfy = config.get("comfy", {})
    workflow_path = WORKFLOW_ROOT / comfy.get(
        "workflow_api_json", "workflows/ltx_i2v_api.json"
    )
    client = ComfyClient(
        base_url=comfy.get("base_url", "http://127.0.0.1:8188"),
        workflow_path=workflow_path,
        input_mapping=comfy.get("inputs", {}),
        output_dir=_work_path(state, "production"),
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )
    artifacts: list[dict[str, Any]] = []
    issues: list[str] = []
    for shot in shots:
        if shot.get("media_strategy") != "video":
            source = shot.get("asset", {}).get("local_path")
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": shot["id"],
                    "kind": "source_image",
                    "path": source,
                    "backend": "source",
                }
            )
            if not source:
                issues.append(f"cut {shot['id']}: 静止画素材がローカルにない")
            continue
        source = shot.get("asset", {}).get("local_path")
        if not source:
            issues.append(f"cut {shot['id']}: ComfyUI入力画像がない")
            continue
        profile = shot.get("generation_profile", {})
        try:
            result = client.generate(
                ComfyGenerationRequest(
                    image_path=source,
                    positive_prompt=shot["positive_prompt"],
                    negative_prompt=shot.get("negative_prompt", ""),
                    width=int(profile.get("width", 576)),
                    height=int(profile.get("height", 384)),
                    frames=int(profile.get("frames", 49)),
                    steps=int(profile.get("steps", 20)),
                    fps=int(profile.get("fps", 24)),
                    seed=int(time.time_ns() % 2_147_483_647),
                    file_prefix=f"agewec_v2_cut_{shot['id']:02d}",
                )
            )
            artifacts.append(
                {
                    "phase": "image_video_production",
                    "cut_id": shot["id"],
                    "kind": "video",
                    "path": result["output_path"],
                    "backend": "comfy",
                    "generation": result,
                }
            )
        except Exception as exc:
            issues.append(f"cut {shot['id']}: {type(exc).__name__}: {exc}")
    return artifacts, issues


def image_video_production(state: WorkflowState) -> dict[str, Any]:
    phase = "image_video_production"
    shots = state["phase_results"]["director"]["data"]["shots"]
    backend = state.get("config", {}).get("production", {}).get("backend", "mock")
    if backend == "comfy":
        artifacts, issues = _comfy_production(state, shots)
    else:
        artifacts, issues = _mock_production(state, shots)
    data = {
        "backend": backend,
        "requested_shots": len(shots),
        "completed_artifacts": len(artifacts),
    }
    return _complete(
        state,
        phase,
        summary=f"{backend}バックエンドで{len(artifacts)}件の成果物を作成",
        data=data,
        artifacts=artifacts,
        status="success" if not issues else "error",
        confidence=0.9 if not issues else 0.4,
        blocking_issues=issues,
        warnings=["mockは実メディアを生成しない"] if backend == "mock" else [],
    )


def visual_qa(state: WorkflowState) -> dict[str, Any]:
    phase = "visual_qa"
    production = state["phase_results"]["image_video_production"]
    artifacts = production.get("artifacts", [])
    missing = []
    empty = []
    for artifact in artifacts:
        raw_path = artifact.get("path")
        if not raw_path:
            missing.append(f"cut {artifact.get('cut_id')}: pathなし")
            continue
        path = Path(raw_path)
        if not path.exists():
            missing.append(str(path))
        elif path.is_file() and path.stat().st_size == 0:
            empty.append(str(path))
    issues = [f"成果物が存在しない: {item}" for item in missing]
    issues.extend(f"成果物が空: {item}" for item in empty)
    route = "image_video_production" if issues else "post_production"
    qa = {
        "route": route,
        "checked_artifacts": len(artifacts),
        "checks": ["ファイル存在", "ゼロバイトでないこと"],
        "future_checks": [
            "ffprobeによるduration/fps/codec",
            "VLMによる映像破綻と指示整合性",
        ],
    }
    return _complete(
        state,
        phase,
        summary=(
            "機械検査を通過"
            if not issues
            else f"機械検査で{len(issues)}件の問題を検出"
        ),
        data=qa,
        status="success" if not issues else "error",
        confidence=0.9 if not issues else 0.3,
        blocking_issues=issues,
        warnings=["内容品質のVLM検査は未実装"],
    )


def post_production(state: WorkflowState) -> dict[str, Any]:
    phase = "post_production"
    production_artifacts = state["phase_results"]["image_video_production"].get(
        "artifacts", []
    )
    output_dir = _work_path(state, "post")
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "post_production_plan.json"
    plan = {
        "timeline": [
            {
                "cut_id": artifact.get("cut_id"),
                "source": artifact.get("path"),
                "kind": artifact.get("kind"),
            }
            for artifact in production_artifacts
        ],
        "operations": [
            "全カットを共通解像度・fpsへ正規化",
            "静止画にパン・ズームを適用",
            "動画を絵コンテ順に結合",
            "字幕・ナレーション・BGMをミックス",
        ],
        "implementation": "ffmpeg_pending",
    }
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = {
        "phase": phase,
        "kind": "post_production_plan",
        "path": str(plan_path),
    }
    return _complete(
        state,
        phase,
        summary="タイムラインとFFmpeg後処理計画を生成",
        data=plan,
        artifacts=[artifact],
        confidence=0.8,
        warnings=["実MP4のFFmpeg結合は未実装"],
    )


def review_board(state: WorkflowState) -> dict[str, Any]:
    phase = "review_board"
    post = state["phase_results"]["post_production"]
    asset = state["phase_results"]["asset_curator"]["data"]
    scores = {
        "concept_consistency": 4,
        "story_structure": 4,
        "asset_traceability": 4 if asset.get("selected_assets") else 1,
        "technical_completion": (
            2 if post["data"].get("implementation") == "ffmpeg_pending" else 5
        ),
    }
    average = round(sum(scores.values()) / len(scores), 2)
    board = {
        "rubric_scores_5": scores,
        "average": average,
        "verdict": "pass",
        "recommendations": [
            "提出前に素材利用条件を最終確認する",
            "FFmpeg実装後に技術完成度を再採点する",
        ],
    }
    return _complete(
        state,
        phase,
        summary=f"Review Board評価 {average}/5、最終承認へ",
        data=board,
        confidence=0.78,
        warnings=board["recommendations"],
    )


def _sanitized(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("api_key", "token", "secret")):
                clean[key] = "***"
            else:
                clean[key] = _sanitized(child)
        return clean
    if isinstance(value, list):
        return [_sanitized(item) for item in value]
    return value


def provenance(state: WorkflowState) -> dict[str, Any]:
    phase = "provenance"
    configured = state.get("config", {}).get("paths", {}).get(
        "provenance_file", "work/provenance.json"
    )
    path = WORKFLOW_ROOT / configured
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": state.get("run_id"),
        "project": state.get("project"),
        "config": _sanitized(state.get("config", {})),
        "phase_results": state.get("phase_results", {}),
        "reviews": state.get("reviews", []),
        "events": state.get("events", []),
        "artifacts": state.get("artifacts", []),
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = {
        "phase": phase,
        "kind": "provenance",
        "path": str(path),
    }
    update = _complete(
        state,
        phase,
        summary=f"証跡を{path.name}へ保存",
        data={"record_path": str(path)},
        artifacts=[artifact],
        confidence=1.0,
    )
    update["final_output"] = str(path)
    return update
