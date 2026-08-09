"""Writer/Storyboard role and duration normalization helpers."""
from __future__ import annotations

import re
from typing import Any

from ..fallbacks import planning as deterministic
from ..state import WorkflowState

from .common import (
    _approved_project_brief, _result_data, _run_role,
)

_JAPANESE_CHARACTER = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def _fallback_japanese_narration(cut: dict[str, Any]) -> str:
    role = str(cut.get("visual_role", "")).lower()
    time_of_day = str(cut.get("time_of_day", "")).lower()
    if role in {"opening", "intro", "introduction"}:
        return "北九州の物語が、ここから始まります。"
    if role in {"climax", "finale"} or time_of_day in {
        "night",
        "evening",
        "blue_hour",
    }:
        return "光に包まれた北九州が、夜空に輝きます。"
    if role in {"ending", "closing", "final"}:
        return "心に残る北九州へ、訪れてみませんか。"
    return "街の営みが、北九州の魅力をつないでいます。"


def _fit_japanese_narration(text: str, allowance: int) -> str:
    compact = "".join(str(text).split())
    if len(compact) <= allowance:
        return compact
    if allowance <= 1:
        return compact[:allowance]
    body_limit = allowance - 1
    candidate = compact[:body_limit]
    # 十分な長さの句点があれば、文の途中ではなくそこで切る。
    punctuation = max(
        candidate.rfind("。"),
        candidate.rfind("！"),
        candidate.rfind("？"),
    )
    if punctuation >= max(1, body_limit // 2):
        return candidate[: punctuation + 1]
    return candidate.rstrip("、。！？") + "。"


def _normalize_japanese_narration(
    cut: dict[str, Any],
    *,
    allowance: int,
) -> tuple[str, list[str]]:
    original = "".join(str(cut.get("narration", "")).split())
    reasons: list[str] = []
    normalized = original
    if (
        not _JAPANESE_CHARACTER.search(original)
        or _ASCII_LETTER.search(original)
    ):
        normalized = _fallback_japanese_narration(cut)
        reasons.append("non_japanese_replaced")
    fitted = _fit_japanese_narration(normalized, allowance)
    if fitted != normalized:
        reasons.append("duration_fit_shortened")
    return fitted, reasons


# --- time_of_day 語彙の正規化 -------------------------------------------------
# LLMは「朝」「morning」「夕暮れ」など多様な表記を返す。下流（素材選定・レポート）は
# 正規化済みの day / dusk / night / unspecified だけを扱う。
TIME_OF_DAY_VALUES = ("day", "dusk", "night", "unspecified")

_TOD_ALIASES: dict[str, str] = {}
for _canonical, _aliases in {
    # 朝・早朝は day へ丸める（素材が薄く、独立させると候補が枯れるため）
    "day": (
        "day", "daytime", "daylight", "morning", "dawn", "sunrise", "noon",
        "midday", "afternoon", "昼", "日中", "昼間", "朝", "早朝", "午前",
        "午後", "夜明け", "明け方", "日の出",
    ),
    "dusk": (
        "dusk", "evening", "sunset", "twilight", "goldenhour", "bluehour",
        "夕", "夕方", "夕暮れ", "夕景", "日没", "黄昏", "たそがれ", "薄暮",
    ),
    "night": (
        "night", "nighttime", "midnight", "夜", "夜間", "深夜", "夜景",
        "ライトアップ", "イルミネーション",
    ),
}.items():
    for _alias in _aliases:
        _TOD_ALIASES[_alias] = _canonical


def normalize_time_of_day(raw: Any) -> str:
    """任意の time_of_day 表記を day / dusk / night へ正規化する。

    判定できない表現は無理に分類せず ``unspecified`` を返し、呼び出し側で
    警告付きフォールバックへ回す。
    """
    if not raw:
        return "unspecified"
    text = str(raw).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if text in _TOD_ALIASES:
        return _TOD_ALIASES[text]
    # 部分一致（"early morning" / "夜の街" のような複合表記を拾う）
    for alias, canonical in _TOD_ALIASES.items():
        if alias in text:
            return canonical
    return "unspecified"


def _rescale_cut_durations(
    cuts: list[dict[str, Any]],
    *,
    target_seconds: float,
    warning_threshold_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Proportionally force positive cut durations to the exact target."""
    normalized = [dict(cut) for cut in cuts]
    original_total = sum(float(cut["seconds"]) for cut in normalized)
    if original_total <= 0:
        raise ValueError("cut duration total must be greater than zero")
    factor = target_seconds / original_total
    original_values = [float(cut["seconds"]) for cut in normalized]
    adjusted_values = [
        round(value * factor, 6)
        for value in original_values
    ]
    # The final cut absorbs floating-point rounding only.  The creative timing
    # change itself is distributed proportionally across every cut.
    rounding_delta = round(
        target_seconds - sum(adjusted_values),
        6,
    )
    adjusted_values[-1] = round(
        adjusted_values[-1] + rounding_delta,
        6,
    )
    if any(value <= 0 for value in adjusted_values):
        raise ValueError(
            "proportional duration correction produced a non-positive cut"
        )
    changes = []
    for cut, original, adjusted in zip(
        normalized,
        original_values,
        adjusted_values,
    ):
        cut["seconds"] = adjusted
        changes.append(
            {
                "cut_id": int(cut["id"]),
                "original_seconds": original,
                "adjusted_seconds": adjusted,
                "delta_seconds": round(adjusted - original, 6),
            }
        )
    total_delta = round(target_seconds - original_total, 6)
    applied = abs(total_delta) > 0.001
    return normalized, {
        "applied": applied,
        "method": (
            "proportional_scale"
            if applied
            else "none"
        ),
        "target_seconds": target_seconds,
        "original_cut_total_seconds": original_total,
        "adjusted_cut_total_seconds": round(
            sum(float(cut["seconds"]) for cut in normalized),
            6,
        ),
        "total_delta_seconds": total_delta,
        "scale_factor": round(factor, 8),
        "rounding_delta_seconds": rounding_delta,
        "large_adjustment_warning": (
            abs(total_delta) > warning_threshold_seconds
        ),
        "warning_threshold_seconds": warning_threshold_seconds,
        "cut_changes": changes,
    }


_FIXED_CUT_DEFAULTS = {
    "media_requirement": "video_required",
    "time_of_day": "night",
    "visual_role": "development",
    "location": "北九州市",
    "subject": "夜景",
    "narration": "",
}


def _fixed_storyboard_cuts(
    state: WorkflowState,
) -> list[dict[str, Any]] | None:
    """Return a human-authored storyboard from config, if one is present.

    `storyboard.fixed_cuts` が空・未設定なら None を返し、従来どおり
    LLMに書かせる。既存の設定を壊さないための既定である。
    """
    raw = (
        state.get("config", {})
        .get("storyboard", {})
        .get("fixed_cuts")
    )
    if not raw:
        return None
    cuts: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        cut = {**_FIXED_CUT_DEFAULTS, **dict(item)}
        cut["id"] = int(cut.get("id", index))
        cut["seconds"] = float(cut["seconds"])
        cut["name"] = str(cut["name"])
        cut["scene"] = str(cut["scene"])
        cut["narration"] = str(cut.get("narration") or "")
        cuts.append(cut)
    cuts.sort(key=lambda c: c["id"])
    ids = [c["id"] for c in cuts]
    if len(set(ids)) != len(ids):
        raise ValueError(f"storyboard.fixed_cuts のidが重複しています: {ids}")
    if any(c["seconds"] <= 0 for c in cuts):
        raise ValueError("storyboard.fixed_cuts の seconds は正の数にしてください")
    return cuts


def _complete_fixed_storyboard(
    state: WorkflowState,
    cuts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use the configured storyboard verbatim, without calling the LLM.

    尺の再スケールも行わない。人間が書いた秒数をそのまま使う
    （合計が目標尺と違う場合は警告として残し、判断は人間に委ねる）。
    """
    total = round(sum(float(c["seconds"]) for c in cuts), 3)
    target = float(
        _approved_project_brief(state).get("target_duration_seconds", total)
    )
    warnings: list[str] = []
    if abs(total - target) > 1e-6:
        warnings.append(
            f"固定絵コンテの合計{total:g}秒が目標尺{target:g}秒と一致しません"
            "（configの seconds をそのまま使用します）"
        )
    max_cuts_value = (
        state.get("config", {})
        .get("production", {})
        .get("max_video_cuts_per_run")
    )
    if max_cuts_value is not None and len(cuts) > int(max_cuts_value):
        warnings.append(
            f"固定絵コンテは{len(cuts)}カットですが "
            f"max_video_cuts_per_run は {int(max_cuts_value)} です。"
            "間引かずに全カットを使用します"
        )
    return deterministic._complete(
        state,
        "writer_storyboard",
        summary=f"設定済みの絵コンテを採用（{len(cuts)}カット、{total:g}秒）",
        data={
            "total_seconds": total,
            "cuts": cuts,
            "source": "config.storyboard.fixed_cuts",
            "duration_adjustment": {"applied": False, "reason": "人間が指定した尺"},
            "cut_limit": {"applied": False, "configured_max_cuts": max_cuts_value},
        },
        confidence=1.0,
        warnings=warnings,
    )


def writer_storyboard(state: WorkflowState) -> dict[str, Any]:
    brief = _approved_project_brief(state)
    concept = _result_data(state, "creative_director")
    if not brief or not concept:
        return deterministic._complete(
            state,
            "writer_storyboard",
            summary="上流企画が不足しているため実行不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["ProjectBriefとCreativeConceptが必要"],
        )

    max_cuts_value = (
        state.get("config", {})
        .get("production", {})
        .get("max_video_cuts_per_run")
    )
    max_cuts = int(max_cuts_value) if max_cuts_value is not None else None

    # 使う写真が先に決まっている制作では、絵コンテをLLMに書かせる意味がない。
    # 毎回抽象的な内容が出て、毎回人間が差し戻すことになるため、
    # config に書いた内容をそのまま採用できるようにする。
    fixed = _fixed_storyboard_cuts(state)
    if fixed is not None:
        return _complete_fixed_storyboard(state, fixed)

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        target = float(brief.get("target_duration_seconds", 30))
        reported_total = float(data["total_seconds"])
        storyboard_config = (
            state.get("config", {}).get("storyboard", {})
        )
        warning_threshold = float(
            storyboard_config.get(
                "duration_large_adjustment_warning_seconds",
                storyboard_config.get(
                    "duration_auto_adjust_tolerance_seconds",
                    2.0,
                ),
            )
        )
        limited_cuts, cut_limit = deterministic._limit_storyboard_cuts(
            data["cuts"],
            max_cuts,
        )
        cuts, duration_adjustment = _rescale_cut_durations(
            limited_cuts,
            target_seconds=target,
            warning_threshold_seconds=warning_threshold,
        )
        duration_adjustment.update(
            {
                "reported_total_seconds": reported_total,
                "reported_total_delta_seconds": round(
                    target - reported_total,
                    6,
                ),
            }
        )
        narration_rate = float(
            storyboard_config.get(
                "max_narration_characters_per_second",
                8,
            )
        )
        narration_language = str(
            storyboard_config.get("narration_language", "ja")
        ).lower()
        auto_normalize = bool(
            storyboard_config.get("auto_normalize_narration", True)
        )
        narration_issues = []
        narration_adjustments: list[dict[str, Any]] = []
        for cut in cuts:
            original = str(cut["narration"])
            allowance = max(1, int(float(cut["seconds"]) * narration_rate))
            if narration_language == "ja" and auto_normalize:
                normalized, reasons = _normalize_japanese_narration(
                    cut,
                    allowance=allowance,
                )
                cut["narration"] = normalized
                if reasons:
                    narration_adjustments.append(
                        {
                            "cut_id": int(cut["id"]),
                            "reasons": reasons,
                            "original": original,
                            "normalized": normalized,
                            "allowance_characters": allowance,
                        }
                    )
                continue
            compact = "".join(original.split())
            if len(compact) > allowance:
                narration_issues.append(
                    f"cut {cut['id']}: narration {len(compact)} chars "
                    f"exceeds {allowance}"
                )
        if narration_issues:
            raise ValueError("; ".join(narration_issues))
        # time_of_day をここで一度だけ正規化する（下流は正規化済みの値のみ扱う）
        time_of_day_normalizations: list[dict[str, Any]] = []
        for cut in cuts:
            raw = cut.get("time_of_day")
            canonical = normalize_time_of_day(raw)
            if str(raw or "") != canonical:
                time_of_day_normalizations.append(
                    {
                        "cut_id": cut.get("id"),
                        "raw": raw,
                        "normalized": canonical,
                    }
                )
            cut["time_of_day"] = canonical
        unspecified_cuts = [
            cut.get("id") for cut in cuts
            if cut.get("time_of_day") == "unspecified"
        ]
        return {
            **data,
            "cuts": cuts,
            "cut_limit": cut_limit,
            "total_seconds": target,
            "duration_source": "project_brief.target_duration_seconds",
            "duration_adjustment": duration_adjustment,
            "narration_language": narration_language,
            "narration_adjustments": narration_adjustments,
            "time_of_day_normalizations": time_of_day_normalizations,
            "time_of_day_unspecified_cut_ids": unspecified_cuts,
        }

    return _run_role(
        state,
        phase="writer_storyboard",
        upstream={
            "project_brief": brief,
            "creative_concept": concept,
            "storyboard_constraints": {
                "max_cuts": max_cuts,
                "instruction": (
                    "Do not output more than max_cuts storyboard cuts."
                ),
            },
        },
        summary=lambda data: (
            f"{len(data['cuts'])}カット、約{data['total_seconds']}秒をLLMが構成"
        ),
        fallback=deterministic.writer_storyboard,
        transform=transform,
    )
