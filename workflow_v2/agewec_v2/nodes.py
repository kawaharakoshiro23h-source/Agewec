"""Role nodes for the isolated AGEWEC v2 workflow.

【本番経路: 現役（ただし単独では使わない）】決定論フォールバック＋共有ヘルパ。

    呼ばれる側: nodes_llm（`deterministic` として）／pipeline_runtime
    直接グラフに接続してはいけない。グラフが使うのは nodes_runtime。

役割:
  1. LLM未使用/失敗時の決定論フォールバック実装
  2. 共有ヘルパ（_load_catalog / _local_asset_path / _complete / AWARD_GENRES 等）

各関数は安定した構造化契約を持つので、LLM/VLM/ツール実装に内部を差し替えても
グラフのルーティングやReview Gateに影響しない。
※ 本番から呼ばれない旧実装が一部残る（[LEGACY 未使用] 印を参照）。
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
    """実行単位（run_id）で分離された作業ディレクトリのパスを返す。

        work/runs/<run_id>/<parts...>

    実行ごとにフォルダを分けることで、過去runとの上書き・混同を防ぐ。
    複数モデルを比較する際も、成果物が互いに潰し合わない。
    run_id が無い場合（単体テスト等）は従来どおり work/ 直下を使う。
    """
    paths = state.get("config", {}).get("paths", {})
    relative = paths.get("work_dir", "work")
    path = WORKFLOW_ROOT / relative
    run_id = str(state.get("run_id") or "")
    if run_id:
        path = path / paths.get("runs_dir", "runs") / run_id
    for part in parts:
        path /= part
    return path


def _cut_path(state: WorkflowState, cut_id: int, *parts: str) -> Path:
    """カット単位のディレクトリ（work/runs/<run_id>/cuts/cut_XX/...）。"""
    return _work_path(state, "cuts", f"cut_{int(cut_id):02d}", *parts)


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
    duration = float(project.get("target_duration_seconds", 30))
    brief = {
        "objective": project.get("theme", "北九州の魅力を世界へ"),
        "target_award": project.get("target_award", "夜景賞"),
        "target_duration_seconds": duration,
        "audience": "北九州をまだ訪れたことのない国内外の旅行者",
        "deliverable": f"{duration:g}秒の観光プロモーション動画",
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
        "source_project": project,
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
            "continuity_rule": "夜へ向かう時間軸と光のモチーフを維持する",
        },
        "camera_intent": {
            "viewer_experience": "昼の活気から荘厳な夜景へ導く",
            "energy_curve": "active_to_calm",
            "stability": "mostly_stable",
            "continuity": "カット間の移動方向と速度を自然につなぐ",
            "hard_constraints": [
                "激しい回転を避ける",
                "実在する建築と地形を維持する",
            ],
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


def _limit_storyboard_cuts(
    cuts: list[dict[str, Any]],
    max_cuts: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Limit a storyboard while preserving its opening-to-climax arc.

    The old ``max_video_cuts_per_run`` setting was only consumed by a legacy
    batch helper.  The active graph needs the limit at the storyboard boundary
    so every downstream phase agrees on the same set of cut IDs.
    """
    original_count = len(cuts)
    if max_cuts is None:
        return [dict(cut) for cut in cuts], {
            "configured_max_cuts": None,
            "original_cut_count": original_count,
            "final_cut_count": original_count,
            "applied": False,
            "dropped_original_cut_ids": [],
        }
    if max_cuts < 1:
        raise ValueError("production.max_video_cuts_per_run must be at least 1")
    if original_count <= max_cuts:
        return [dict(cut) for cut in cuts], {
            "configured_max_cuts": max_cuts,
            "original_cut_count": original_count,
            "final_cut_count": original_count,
            "applied": False,
            "dropped_original_cut_ids": [],
        }

    if max_cuts == 1:
        indices = [original_count - 1]
    else:
        indices = [
            round(position * (original_count - 1) / (max_cuts - 1))
            for position in range(max_cuts)
        ]
    selected = []
    selected_indices = set(indices)
    for new_id, index in enumerate(indices, start=1):
        cut = dict(cuts[index])
        cut["original_cut_id"] = int(cut["id"])
        cut["id"] = new_id
        selected.append(cut)
    dropped = [
        int(cut["id"])
        for index, cut in enumerate(cuts)
        if index not in selected_indices
    ]
    return selected, {
        "configured_max_cuts": max_cuts,
        "original_cut_count": original_count,
        "final_cut_count": len(selected),
        "applied": True,
        "selected_original_cut_ids": [
            int(cut["original_cut_id"]) for cut in selected
        ],
        "dropped_original_cut_ids": dropped,
        "selection_method": "evenly_spaced_preserve_opening_and_climax",
    }


def writer_storyboard(state: WorkflowState) -> dict[str, Any]:
    phase = "writer_storyboard"
    duration = float(
        state.get("project", {}).get("target_duration_seconds", 30)
    )
    base_cuts = [
        (
            "導入",
            "昼の北九州で人々の活動が始まる",
            "今日も、北九州から新しい一日が始まる。",
            "day",
            "opening",
            "北九州市街",
            "街と人の活動",
        ),
        (
            "港",
            "港と海を行き交う船や物流",
            "海とともに育った街。",
            "day",
            "expansion",
            "北九州港",
            "海と港湾",
        ),
        (
            "産業",
            "工場群と都市の営み",
            "ものづくりの力が、未来を動かす。",
            "late_afternoon",
            "development",
            "工場地帯",
            "産業景観",
        ),
        (
            "歴史",
            "歴史的建築と現代の街並み",
            "受け継いだ時間が、新しい景色をつくる。",
            "sunset",
            "transition",
            "門司港・小倉",
            "歴史的建築",
        ),
        (
            "人と街",
            "灯り始めた街を人々が行き交う",
            "ここには、暮らしの温度がある。",
            "blue_hour",
            "emotional_bridge",
            "小倉都心部",
            "人と交通",
        ),
        (
            "締め",
            "皿倉山から広がる荘厳な北九州の夜景",
            "光の先へ。北九州で会いましょう。",
            "night",
            "climax",
            "皿倉山",
            "北九州の夜景",
        ),
    ]
    max_cuts_value = (
        state.get("config", {})
        .get("production", {})
        .get("max_video_cuts_per_run")
    )
    max_cuts = int(max_cuts_value) if max_cuts_value is not None else None
    seconds = duration / len(base_cuts)
    cuts = []
    allocated = 0.0
    for index, (
        name,
        scene,
        narration,
        time_of_day,
        visual_role,
        location,
        subject,
    ) in enumerate(base_cuts, start=1):
        cut_seconds = (
            round(duration - allocated, 2)
            if index == len(base_cuts)
            else round(seconds, 2)
        )
        allocated += cut_seconds
        cuts.append(
            {
                "id": index,
                "name": name,
                "scene": scene,
                "narration": narration,
                "seconds": cut_seconds,
                "media_requirement": "video_required",
                "time_of_day": time_of_day,
                "visual_role": visual_role,
                "location": location,
                "subject": subject,
            }
        )
    cuts, cut_limit = _limit_storyboard_cuts(cuts, max_cuts)
    if cut_limit["applied"]:
        seconds = duration / len(cuts)
        allocated = 0.0
        for index, cut in enumerate(cuts, start=1):
            cut_seconds = (
                round(duration - allocated, 2)
                if index == len(cuts)
                else round(seconds, 2)
            )
            cut["seconds"] = cut_seconds
            allocated += cut_seconds

    storyboard = {
        "total_seconds": float(duration),
        "cuts": cuts,
        "cut_limit": cut_limit,
        "duration_source": "project.target_duration_seconds",
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


def _local_asset_path(photo: dict[str, Any]) -> str | None:
    # 1) カタログに local_path があればそれを優先（新命名 asset-XXX_... に対応）
    rel = photo.get("local_path")
    if rel:
        candidate = PROJECT_ROOT / rel
        if candidate.exists():
            return str(candidate)
    # 2) 後方互換: image_url のファイル名から推測（旧カタログ用）
    image_url = photo.get("image_url", "")
    name = unquote(Path(urlparse(image_url).path).name)
    candidate = PROJECT_ROOT / "assets_dl" / name
    return str(candidate) if name and candidate.exists() else None


def asset_curator(state: WorkflowState) -> dict[str, Any]:
    phase = "asset_curator"
    cuts = (
        state.get("phase_results", {})
        .get("writer_storyboard", {})
        .get("data", {})
        .get("cuts", [])
    )
    catalog = _load_catalog()
    candidates = []
    for index, photo in enumerate(catalog.get("photos", []), start=1):
        local_path = _local_asset_path(photo)
        candidates.append(
            {
                "asset_id": f"asset-{index:03d}",
                "title": photo.get("title", ""),
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": photo.get("genres", []),
                "areas": photo.get("areas", []),
                "local_path": local_path,
                "local_available": bool(local_path),
                "usage_scope": "agewec_submission",
                "rights_status": "approved_for_agewec_submission",
            }
        )
    candidates.sort(
        key=lambda item: (
            not item["local_available"],
            item["asset_id"],
        )
    )
    context = state.get("review_context", {}).get(phase, {})
    target_cut_id = context.get("target_cut_id")
    existing = (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
        .get("asset_assignments", [])
    )
    assignments = {
        int(item["cut_id"]): item
        for item in existing
        if target_cut_id is not None
    }
    for index, cut in enumerate(cuts):
        cut_id = int(cut["id"])
        if target_cut_id is not None and cut_id != int(target_cut_id):
            continue
        if not candidates:
            break
        primary = candidates[index % len(candidates)]
        alternative = candidates[(index + 1) % len(candidates)]
        assignments[cut_id] = {
            "cut_id": cut_id,
            "primary": {
                **primary,
                "selection_reason": (
                    f"{cut['location']}の{cut['visual_role']}に利用する"
                ),
            },
            "alternatives": [
                {
                    **alternative,
                    "selection_reason": "構図または時刻帯の代替候補",
                }
            ]
            if alternative["asset_id"] != primary["asset_id"]
            else [],
        }
    missing_cut_ids = sorted(
        {int(cut["id"]) for cut in cuts} - set(assignments)
    )
    assignment_list = [assignments[key] for key in sorted(assignments)]
    selected = [
        {**item["primary"], "cut_id": item["cut_id"]}
        for item in assignment_list
    ]
    manifest = {
        "catalog_source": catalog.get("source"),
        "available_candidate_count": len(candidates),
        "asset_assignments": assignment_list,
        "selected_assets": selected,
        "unassigned_cut_ids": missing_cut_ids,
        "rights_check_required": False,
        "usage_scope": "agewec_submission",
        "targeted_revision_cut_id": target_cut_id,
    }
    return _complete(
        state,
        phase,
        summary=f"{len(assignment_list)}カットへ公式素材を割当",
        data=manifest,
        confidence=0.88 if not missing_cut_ids else 0.4,
        blocking_issues=(
            []
            if not missing_cut_ids
            else [f"素材未割当カット: {missing_cut_ids}"]
        ),
        warnings=(
            ["一部の選定素材はローカル未取得"]
            if any(not item.get("local_available") for item in selected)
            else []
        ),
    )


def director(state: WorkflowState) -> dict[str, Any]:
    phase = "director"
    cuts = state["phase_results"]["writer_storyboard"]["data"]["cuts"]
    assignments = {
        int(item["cut_id"]): item
        for item in state["phase_results"]["asset_curator"]["data"].get(
            "asset_assignments",
            [],
        )
    }
    camera_intent = (
        state["phase_results"]["creative_director"]["data"]
        .get("camera_intent", {})
    )
    context = state.get("review_context", {}).get(phase, {})
    target_cut_id = context.get("target_cut_id")
    existing = (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
        .get("shots", [])
    )
    shot_map = {
        int(shot["id"]): shot
        for shot in existing
        if target_cut_id is not None
    }
    for cut in cuts:
        cut_id = int(cut["id"])
        if target_cut_id is not None and cut_id != int(target_cut_id):
            continue
        assignment = assignments.get(cut_id, {})
        asset = assignment.get("primary", {})
        motion = "slow stable push-in with subtle parallax"
        shot_map[cut_id] = (
            {
                **cut,
                "asset": asset,
                "positive_prompt": (
                    f"{cut['scene']}. {motion}. Deep blue and warm amber color "
                    "palette, realistic documentary cinematography."
                ),
                "negative_prompt": "",
                "camera_motion": motion,
                "motion_intensity": "subtle",
                "rationale": (
                    "実在景観を維持しながら静止画へ奥行きを加えるため"
                ),
                "camera_intent_alignment": (
                    camera_intent.get(
                        "viewer_experience",
                        "全体の安定した映像方針",
                    )
                ),
                "deviation_reason": None,
            }
        )
    shot_plan = [shot_map[key] for key in sorted(shot_map)]
    missing = sorted(
        {int(cut["id"]) for cut in cuts} - set(shot_map)
    )
    plan = {
        "shots": shot_plan,
        "continuity_checks": [
            "deep blueとwarm amberを維持",
            "建築・地形を過度に変形させない",
            "カメラ移動は緩やかにする",
        ],
        "targeted_revision_cut_id": target_cut_id,
        "technical_parameters_status": "pending_support_video_creator",
    }
    return _complete(
        state,
        phase,
        summary=f"{len(shot_plan)}カットの素材・演出指示を確定",
        data=plan,
        confidence=0.85,
        blocking_issues=(
            [] if not missing else [f"演出未作成カット: {missing}"]
        ),
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
        requires_video = (
            shot.get("media_requirement") == "video_required"
            or shot.get("media_strategy") == "video"
        )
        if not requires_video:
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
        production = config.get("production", {})
        profile_name = str(production.get("profile", "draft"))
        profile = (
            shot.get("generation_profile")
            or production.get("profiles", {}).get(profile_name, {})
        )
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
    """[LEGACY 未使用] 旧・編集計画のみ版（`ffmpeg_pending` を返す）。

    本番は `pipeline_runtime.post_production`（FFmpegで実結合＝`ffmpeg_executed`）。
    互換のため残置。新しい実装はこちらに追加しないこと。
    """
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
