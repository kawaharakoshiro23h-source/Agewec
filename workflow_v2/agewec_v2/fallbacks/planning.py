"""Deterministic planning and storyboard fallbacks."""
from __future__ import annotations

from typing import Any

from ..state import WorkflowState

from .common import _complete

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
