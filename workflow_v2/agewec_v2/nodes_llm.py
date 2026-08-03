"""LLM-connected role nodes.

【本番経路: 現役】役割別のLLM実行（Executive Producer〜Review Board）。

    呼ばれる側: nodes_runtime / pipeline_runtime
    使う側    : nodes.py（`deterministic` としてフォールバック・共有ヘルパを利用）

llm.enabled が false のとき、または LLM 呼び出しに失敗したときは、nodes.py の
決定論版へ委譲する。判断はRoleRunner（LLM）が、メディア/ファイル操作は決定論ツールが担う。
※ このファイル内に本番から呼ばれない旧実装が一部残る（[LEGACY 未使用] 印を参照）。
"""
from __future__ import annotations

import copy
import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import nodes as deterministic
from .llm import LLMSettings, RoleRunner
from .state import WorkflowState


def _result_data(state: WorkflowState, phase: str) -> dict[str, Any]:
    return (
        state.get("phase_results", {})
        .get(phase, {})
        .get("data", {})
    )


def _approved_project_brief(state: WorkflowState) -> dict[str, Any]:
    """下流LLMへ渡す、承認済みの企画契約を返す。

    `source_project`は証跡用の初期入力であり、承認後の指示ではない。
    保存済みProjectBrief自体は変更せず、LLM入力用のコピーから
    のみ除外する。
    """
    brief = copy.deepcopy(_result_data(state, "executive_producer"))
    brief.pop("source_project", None)
    return brief


def _approved_project_value(
    state: WorkflowState,
    key: str,
    default: Any,
) -> Any:
    """ProjectBriefを優先し、未生成時だけ初期projectへ戻る。"""
    brief = _result_data(state, "executive_producer")
    if key in brief:
        return brief[key]
    return state.get("project", {}).get(key, default)


def _feedback(state: WorkflowState, phase: str) -> str:
    return state.get("feedback", {}).get(phase, "")


def _review_context(
    state: WorkflowState,
    phase: str,
) -> dict[str, Any]:
    return dict(state.get("review_context", {}).get(phase, {}))


def _llm_settings(state: WorkflowState) -> LLMSettings:
    return LLMSettings.from_sources(state.get("config", {}))


def _with_llm_metadata(
    update: dict[str, Any],
    phase: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    phase_results = dict(update["phase_results"])
    result = dict(phase_results[phase])
    result["llm"] = metadata
    phase_results[phase] = result
    update["phase_results"] = phase_results
    return update


def _with_llm_feedback_status(
    update: dict[str, Any],
    phase: str,
    feedback: str,
) -> dict[str, Any]:
    """Record delivery and observable output change without overclaiming.

    A changed JSON artifact is evidence that a retry produced a new result, but
    it is not proof that every semantic instruction was followed. Human review
    remains the authority for that judgment.
    """
    phase_results = dict(update["phase_results"])
    result = dict(phase_results[phase])
    result["feedback_received"] = feedback
    if feedback:
        previous = result.get("previous_data")
        current = result.get("data")
        result["feedback_applied"] = None
        result["feedback_status"] = "delivered_to_llm_pending_human_verification"
        result["feedback_application_evidence"] = (
            "output_changed"
            if previous is not None and previous != current
            else "output_unchanged_or_no_baseline"
        )
    else:
        result["feedback_applied"] = False
        result["feedback_status"] = "not_provided"
    phase_results[phase] = result
    update["phase_results"] = phase_results
    return update


def _llm_error(
    state: WorkflowState,
    phase: str,
    exc: Exception,
) -> dict[str, Any]:
    return deterministic._complete(
        state,
        phase,
        summary=f"LLM実行失敗: {type(exc).__name__}",
        data={},
        status="error",
        confidence=0.0,
        blocking_issues=[f"{type(exc).__name__}: {exc}"],
    )


def _run_role(
    state: WorkflowState,
    *,
    phase: str,
    upstream: dict[str, Any],
    summary: Callable[[dict[str, Any]], str],
    fallback: Callable[[WorkflowState], dict[str, Any]],
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    try:
        settings = _llm_settings(state)
    except Exception as exc:
        return _llm_error(state, phase, exc)
    if not settings.enabled:
        return fallback(state)

    try:
        feedback = _feedback(state, phase)
        run = RoleRunner(state.get("config", {})).run(
            role=phase,
            upstream=upstream,
            feedback=feedback,
        )
        raw = run.output.model_dump(mode="json")
        data = transform(raw) if transform else raw
        update = deterministic._complete(
            state,
            phase,
            summary=summary(data),
            data=data,
            artifacts=artifacts,
            confidence=float(data.get("confidence", 0.9)),
            warnings=warnings,
        )
        update = _with_llm_feedback_status(update, phase, feedback)
        return _with_llm_metadata(update, phase, run.metadata)
    except Exception as exc:
        if settings.strict_mode:
            return _llm_error(state, phase, exc)
        update = fallback(state)
        result = dict(update["phase_results"][phase])
        result["warnings"] = list(result.get("warnings", [])) + [
            f"LLM失敗のため決定的フォールバックを使用: {type(exc).__name__}: {exc}"
        ]
        result["llm"] = {
            "provider": settings.provider,
            "model": settings.model,
            "fallback": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        phase_results = dict(update["phase_results"])
        phase_results[phase] = result
        update["phase_results"] = phase_results
        return update


def executive_producer(state: WorkflowState) -> dict[str, Any]:
    project = state.get("project", {})

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        requested_duration = float(
            project.get("target_duration_seconds", 30)
        )
        if abs(float(data["target_duration_seconds"]) - requested_duration) > 0.01:
            raise ValueError(
                "Executive Producer must preserve "
                f"target_duration_seconds={requested_duration}"
            )
        requested_award = str(project.get("target_award", ""))
        if requested_award and data["target_award"] != requested_award:
            raise ValueError(
                f"Executive Producer must preserve target_award={requested_award}"
            )
        return {
            **data,
            "target_duration_seconds": requested_duration,
            "source_project": project,
        }

    return _run_role(
        state,
        phase="executive_producer",
        upstream={
            "project": project,
            "system_capabilities": {
                "orchestrator": "LangGraph",
                "media_backend": state.get("config", {})
                .get("production", {})
                .get("backend", "mock"),
                "review_modes": ["always", "on_exception", "never"],
            },
        },
        summary=lambda data: f"{data['deliverable']}の制作方針をLLMが定義",
        fallback=deterministic.executive_producer,
        transform=transform,
    )


def creative_director(state: WorkflowState) -> dict[str, Any]:
    brief = _approved_project_brief(state)
    if not brief:
        return deterministic._complete(
            state,
            "creative_director",
            summary="上流ProjectBriefがないため実行不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["executive_producerの有効な出力が必要"],
        )

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        # ProjectBriefの成功基準は、LLMによる転記や言い換えに依存させない。
        # 上流基準を先頭に固定し、Creative Director独自の追加基準だけを
        # 後ろへ残すことで、契約を確実に継承しつつ創造的な追加を許可する。
        inherited = [
            str(item).strip()
            for item in brief.get("success_criteria", [])
            if str(item).strip()
        ]
        proposed = [
            str(item).strip()
            for item in data.get("success_criteria", [])
            if str(item).strip()
        ]
        merged = list(dict.fromkeys([*inherited, *proposed]))
        return {
            **data,
            "success_criteria": merged,
            "inherited_success_criteria": inherited,
        }

    return _run_role(
        state,
        phase="creative_director",
        upstream={
            "project_brief": brief,
        },
        summary=lambda data: f"コンセプト「{data['title']}」をLLMが策定",
        fallback=deterministic.creative_director,
        transform=transform,
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


def _asset_candidates(state: WorkflowState) -> list[dict[str, Any]]:
    award = _approved_project_value(state, "target_award", "夜景賞")
    target_genre = deterministic.AWARD_GENRES.get(award)
    catalog = deterministic._load_catalog()
    photos = catalog.get("photos", [])
    candidates = []
    for index, photo in enumerate(photos, start=1):
        title = str(photo.get("title", ""))
        genres = list(photo.get("genres", []))
        local_path = deterministic._local_asset_path(photo)
        file_size = None
        sha256 = None
        acquired_at = None
        if local_path:
            path = Path(local_path)
            stat = path.stat()
            file_size = stat.st_size
            acquired_at = datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        time_of_day = _asset_time_of_day(title, genres)
        candidates.append(
            {
                "asset_id": f"asset-{index:03d}",
                "title": title,
                "source_url": photo.get("image_url", ""),
                "detail_url": photo.get("detail_url", ""),
                "genres": genres,
                "areas": photo.get("areas", []),
                "local_path": local_path,
                "local_available": bool(local_path),
                "time_of_day": time_of_day,
                "visual_roles": genres,
                "target_award_match": (
                    target_genre is None or target_genre in genres
                ),
                "usage_scope": "agewec_submission",
                "rights_status": "approved_for_agewec_submission",
                "file_size_bytes": file_size,
                "sha256": sha256,
                "acquired_at": acquired_at,
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            not item["local_available"],
            not item["target_award_match"],
            item["asset_id"],
        ),
    )


_CLIMAX_ROLES = {"climax", "ending", "final", "finale", "closing"}


_ASSET_DUSK_TERMS = ("夕", "夕暮", "夕景", "サンセット", "黄昏", "日没", "薄暮")
_ASSET_NIGHT_TERMS = ("夜", "ライトアップ", "イルミネーション")


def _asset_time_of_day(title: str, genres: list[str]) -> str:
    """素材（写真）の時間帯を night / dusk / unknown_or_day に分類する。

    タイトルとジャンルからの推定であり、断定ではない。判定できないものは
    ``unknown_or_day`` として扱う（昼として使えることが多いため）。
    """
    if any(term in title for term in _ASSET_DUSK_TERMS):
        return "dusk"
    if any(term in title for term in _ASSET_NIGHT_TERMS):
        return "night"
    if "イルミネーション・夜景" in genres:
        return "night"
    return "unknown_or_day"


def _tod_eval(cut_tod: str, cand_tod: str) -> tuple[bool, int]:
    """time_of_day の相性（最優先シグナル）。(除外するか, 加点) を返す。

    カット側は正規化済み（day / dusk / night / unspecified）。
    素材側は night / dusk / unknown_or_day。
    完全一致→優先 / 隣接（dusk↔day,night）→許容 / day↔night→原則除外 /
    unspecified→減点して残す。
    """
    cand_night = cand_tod == "night"
    cand_dusk = cand_tod == "dusk"
    if cut_tod == "day":
        if cand_night:
            return (True, 0)          # 昼カットに夜景は除外（最重要）
        if cand_dusk:
            return (False, 1)         # 隣接: 許容するが低め
        return (False, 4)             # unknown_or_day
    if cut_tod == "night":
        if cand_night:
            return (False, 6)
        if cand_dusk:
            return (False, 2)         # 隣接: 許容
        return (False, -2)            # 昼素材は減点して残す
    if cut_tod == "dusk":
        if cand_dusk:
            return (False, 6)         # 完全一致を最優先
        return (False, 2)             # 昼夜どちらも隣接として許容
    return (False, 0)                 # unspecified: 素通り（緩和側で拾う）


def _location_score(cut_location: str, areas: list[str]) -> int:
    if not cut_location:
        return 0
    for area in areas:
        base = area.replace("エリア", "")
        for seg in base.split("・") + [base]:
            if seg and (seg in cut_location or cut_location in seg):
                return 3
    return 0


def _subject_score(cut: dict[str, Any], title: str) -> int:
    text = f"{cut.get('subject', '')}{cut.get('name', '')}"
    grams = {text[i:i + 2] for i in range(len(text) - 1)}
    return 1 if any(g in title for g in grams) else 0


def _shortlist_candidates(cuts: list[dict[str, Any]],
                          candidates: list[dict[str, Any]],
                          award: str, per_cut: int = 8) -> list[dict[str, Any]]:
    """285件を「カット別スコア付きユニオン」へ絞る。

    local_available をハードフィルタ、time_of_day を最優先に採点し、各カット上位
    per_cut 件を統合。候補には eligible_cut_ids / scores_by_cut を付与する。
    """
    local = [c for c in candidates if c.get("local_available")]  # ハードフィルタ
    award_genre = deterministic.AWARD_GENRES.get(award)
    result: dict[str, dict[str, Any]] = {}
    relaxed_cut_ids: list[int] = []

    for cut in cuts:
        cut_id = int(cut["id"])
        cut_tod = str(cut.get("time_of_day", "unspecified"))
        cut_loc = str(cut.get("location", ""))
        role = str(cut.get("visual_role", ""))

        scored, relaxed = [], []
        for c in local:
            excluded, s = _tod_eval(cut_tod, c.get("time_of_day", "unknown_or_day"))
            s += _location_score(cut_loc, c.get("areas", []))
            if award_genre and award_genre in c.get("genres", []):
                s += 3 if role in _CLIMAX_ROLES else 1  # 賞ジャンルはclimaxで強く
            s += _subject_score(cut, c.get("title", ""))
            relaxed.append((s, c))
            if not excluded:
                scored.append((s, c))
        # 時間帯の除外で候補が枯れた場合は停止せず、隣接時間帯まで緩和する
        pool = scored
        if not pool:
            pool = relaxed
            relaxed_cut_ids.append(cut_id)
        pool.sort(key=lambda x: (-x[0], x[1]["asset_id"]))

        # 多様性キャップ: 同一エリア先頭 / タイトル接頭辞は各2件まで
        picked, area_ct, pref_ct = [], {}, {}
        for score, c in pool:
            area_key = (c.get("areas") or ["-"])[0]
            pref = c.get("title", "")[:4]
            if area_ct.get(area_key, 0) >= 2 or pref_ct.get(pref, 0) >= 2:
                continue
            picked.append((score, c))
            area_ct[area_key] = area_ct.get(area_key, 0) + 1
            pref_ct[pref] = pref_ct.get(pref, 0) + 1
            if len(picked) >= per_cut:
                break
        if len(picked) < per_cut:  # キャップで足りなければ補充
            have = {c["asset_id"] for _, c in picked}
            for score, c in pool:
                if c["asset_id"] in have:
                    continue
                picked.append((score, c))
                if len(picked) >= per_cut:
                    break

        for score, c in picked:
            r = result.setdefault(
                c["asset_id"],
                {**c, "eligible_cut_ids": [], "scores_by_cut": {}},
            )
            r["eligible_cut_ids"].append(cut_id)
            r["scores_by_cut"][str(cut_id)] = int(score)

    shortlisted = list(result.values())
    # 緩和が発生したカットを呼び出し側へ伝える（停止させず警告に留めるため）
    _shortlist_candidates.last_relaxed_cut_ids = relaxed_cut_ids
    return shortlisted


def _compact_asset_candidates_for_llm(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only fields needed for semantic selection.

    Provenance fields such as URLs, hashes, file sizes, acquisition timestamps,
    and absolute local paths stay in the full local candidate map.  They are
    restored after the LLM returns asset IDs, so they never consume context.
    """
    fields = (
        "asset_id",
        "title",
        "genres",
        "areas",
        "time_of_day",
        "visual_roles",
        "target_award_match",
        "eligible_cut_ids",
        "scores_by_cut",
    )
    return [
        {
            field: candidate.get(field)
            for field in fields
        }
        for candidate in candidates
    ]


def _canonical_asset_id(value: Any) -> str:
    """Normalize harmless zero-padding variations in LLM asset IDs."""
    raw = str(value).strip()
    match = re.fullmatch(r"asset-(\d+)", raw, flags=re.IGNORECASE)
    if not match:
        return raw
    return f"asset-{int(match.group(1)):03d}"


def _ranked_candidates_for_cut(
    candidates: list[dict[str, Any]],
    cut_id: int,
) -> list[dict[str, Any]]:
    return sorted(
        (
            candidate
            for candidate in candidates
            if cut_id
            in {
                int(value)
                for value in candidate.get("eligible_cut_ids", [])
            }
        ),
        key=lambda candidate: (
            -int(
                candidate.get("scores_by_cut", {}).get(
                    str(cut_id),
                    -10_000,
                )
            ),
            not bool(candidate.get("target_award_match")),
            str(candidate.get("asset_id", "")),
        ),
    )


def _code_asset_reason(
    cut: dict[str, Any],
    candidate: dict[str, Any],
    *,
    source: str,
) -> str:
    score = candidate.get("scores_by_cut", {}).get(
        str(int(cut["id"])),
        "n/a",
    )
    if source == "explicit_feedback":
        return (
            f"人間の明示指定により{candidate.get('title', '')}を採用。"
            f"対象カットの事前適合スコアは{score}。"
        )
    if source == "retry_next_candidate":
        return (
            f"前候補への修正指示を受け、次点の"
            f"{candidate.get('title', '')}を採用（スコア{score}）。"
        )
    return (
        f"{cut.get('time_of_day', '')}・{cut.get('location', '')}・"
        f"{cut.get('visual_role', '')}の条件に対する事前採点で"
        f"{candidate.get('title', '')}が上位（スコア{score}）。"
    )


def _requested_asset_id(feedback: str) -> str | None:
    match = re.search(r"asset-\d+", feedback, flags=re.IGNORECASE)
    return _canonical_asset_id(match.group(0)) if match else None


def asset_curator(state: WorkflowState) -> dict[str, Any]:
    phase = "asset_curator"
    storyboard = _result_data(state, "writer_storyboard")
    if not storyboard:
        return deterministic._complete(
            state,
            phase,
            summary="絵コンテがないため素材選定不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Storyboardが必要"],
        )
    candidates = _asset_candidates(state)
    award = _approved_project_value(state, "target_award", "夜景賞")
    per_cut = int(
        state.get("config", {}).get("assets", {}).get("shortlist_per_cut", 8)
    )
    candidates = _shortlist_candidates(
        storyboard["cuts"], candidates, award, per_cut
    )
    context = _review_context(state, "asset_curator")
    target_cut_id = context.get("target_cut_id")
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)
    cut_map = {
        int(cut["id"]): cut
        for cut in storyboard.get("cuts", [])
    }
    valid_cut_ids = set(cut_map)
    if target_cut_id is not None and target_cut_id not in valid_cut_ids:
        return deterministic._complete(
            state,
            phase,
            summary="対象カットIDが存在しないため素材変更不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=[f"Unknown target_cut_id: {target_cut_id}"],
        )

    existing_items = (
        _result_data(state, phase).get("asset_assignments", [])
        if target_cut_id is not None
        else []
    )
    merged = {
        int(item["cut_id"]): copy.deepcopy(item)
        for item in existing_items
        if int(item["cut_id"]) in valid_cut_ids
    }
    feedback = _feedback(state, phase)
    requested_id = _requested_asset_id(feedback)
    if requested_id and target_cut_id is None:
        return deterministic._complete(
            state,
            phase,
            summary="素材IDの明示変更には対象カットIDが必要",
            data={
                "selection_mode": "deterministic_ranker",
                "requested_asset_id": requested_id,
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                f"{requested_id}を指定する場合はtarget_cut_idも指定してください"
            ],
        )
    alternatives_per_cut = int(
        state.get("config", {})
        .get("assets", {})
        .get("alternatives_per_cut", 2)
    )
    selected_ids = {
        str(item.get("primary", {}).get("asset_id", ""))
        for cut_id, item in merged.items()
        if cut_id != target_cut_id
    }
    selection_cut_ids = (
        [target_cut_id]
        if target_cut_id is not None
        else sorted(valid_cut_ids)
    )
    new_assignments: dict[int, dict[str, Any]] = {}
    blocking: list[str] = []
    warnings: list[str] = []
    # 時間帯が厳密一致しないため隣接時間帯へ緩和したカットを警告として残す
    for relaxed_id in getattr(_shortlist_candidates, "last_relaxed_cut_ids", []):
        warnings.append(
            f"cut {relaxed_id}: 時間帯が一致する素材がないため隣接時間帯へ緩和"
        )
    for cut_id in selection_cut_ids:
        cut = cut_map[cut_id]
        ranked = _ranked_candidates_for_cut(candidates, cut_id)
        if not ranked:
            blocking.append(
                f"cut {cut_id}: eligibleなローカル素材候補が0件"
            )
            continue
        source = "deterministic_ranker"
        previous_id = str(
            merged.get(cut_id, {}).get("primary", {}).get(
                "asset_id",
                "",
            )
        )
        if requested_id:
            requested = next(
                (
                    candidate
                    for candidate in ranked
                    if candidate["asset_id"] == requested_id
                ),
                None,
            )
            if requested is None:
                blocking.append(
                    f"cut {cut_id}: 指定素材{requested_id}は"
                    "このカットのeligible候補ではありません"
                )
                continue
            primary = requested
            source = "explicit_feedback"
        elif target_cut_id is not None and previous_id:
            next_candidates = [
                candidate
                for candidate in ranked
                if candidate["asset_id"] != previous_id
            ]
            if next_candidates:
                primary = next_candidates[0]
                source = "retry_next_candidate"
            else:
                primary = ranked[0]
                warnings.append(
                    f"cut {cut_id}: 次点候補がないため同一素材を維持"
                )
        else:
            unused = [
                candidate
                for candidate in ranked
                if candidate["asset_id"] not in selected_ids
            ]
            primary = (unused or ranked)[0]
        selected_ids.add(primary["asset_id"])
        alternatives = [
            candidate
            for candidate in ranked
            if candidate["asset_id"] != primary["asset_id"]
        ][: max(0, alternatives_per_cut)]
        new_assignments[cut_id] = {
            "cut_id": cut_id,
            "primary": {
                **primary,
                "selection_reason": _code_asset_reason(
                    cut,
                    primary,
                    source=source,
                ),
                "selection_reason_source": "deterministic",
                "llm_rationale": _code_asset_reason(
                    cut,
                    primary,
                    source=source,
                ),
                "rationale_source": "deterministic_fallback",
                "selection_source": source,
            },
            "alternatives": [
                {
                    **candidate,
                    "selection_reason": (
                        f"コード採点による次点候補"
                        f"（スコア"
                        f"{candidate.get('scores_by_cut', {}).get(str(cut_id))}）"
                    ),
                    "selection_reason_source": "deterministic",
                }
                for candidate in alternatives
            ],
        }

    if blocking:
        return deterministic._complete(
            state,
            phase,
            summary="決定論的素材選定で解決不能な条件を検出",
            data={
                "selection_mode": "deterministic_ranker",
                "targeted_revision_cut_id": target_cut_id,
            },
            status="error",
            confidence=0.0,
            blocking_issues=blocking,
            warnings=warnings,
        )

    rationale_metadata: dict[str, Any] = {
        "decision_owner": "deterministic_ranker",
        "role": "asset_curator_rationale",
        "fallback": True,
        "reason": "LLM disabled",
    }
    rationale_map: dict[int, str] = {}
    try:
        settings = _llm_settings(state)
        if settings.enabled:
            rationale_input = [
                {
                    "cut": cut_map[cut_id],
                    "selected_asset": _compact_asset_candidates_for_llm(
                        [assignment["primary"]]
                    )[0],
                    "deterministic_reason": assignment["primary"][
                        "selection_reason"
                    ],
                }
                for cut_id, assignment in sorted(new_assignments.items())
            ]
            run = RoleRunner(state.get("config", {})).run(
                role="asset_curator_rationale",
                upstream={
                    "project_brief": _approved_project_brief(state),
                    "final_selections": rationale_input,
                    "instruction": (
                        "IDs are immutable. Explain only the supplied decisions."
                    ),
                },
                feedback=feedback,
            )
            valid_reason_ids = set(new_assignments)
            rationale_map = {
                int(item["cut_id"]): str(item["reason"]).strip()
                for item in run.output.model_dump(mode="json")[
                    "rationales"
                ]
                if int(item["cut_id"]) in valid_reason_ids
                and str(item["reason"]).strip()
            }
            rationale_metadata = {
                **run.metadata,
                "decision_owner": "deterministic_ranker",
                "role": "asset_curator_rationale",
                "fallback": len(rationale_map) != len(new_assignments),
            }
            if len(rationale_map) != len(new_assignments):
                warnings.append(
                    "LLM理由が一部不足したためコード生成理由を使用"
                )
    except Exception as exc:
        rationale_metadata = {
            "provider": (
                settings.provider
                if "settings" in locals()
                else "unknown"
            ),
            "model": (
                settings.model
                if "settings" in locals()
                else ""
            ),
            "decision_owner": "deterministic_ranker",
            "role": "asset_curator_rationale",
            "fallback": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        warnings.append(
            "LLM理由生成に失敗したためコード生成理由で続行: "
            f"{type(exc).__name__}"
        )

    for cut_id, reason in rationale_map.items():
        new_assignments[cut_id]["primary"]["llm_rationale"] = reason
        new_assignments[cut_id]["primary"]["rationale_source"] = "llm"
    merged.update(new_assignments)
    missing_cuts = sorted(valid_cut_ids - set(merged))
    if missing_cuts:
        return deterministic._complete(
            state,
            phase,
            summary="全カットへPrimary素材を割り当てられない",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=[
                "Primary素材がないカット: "
                + ", ".join(map(str, missing_cuts))
            ],
        )
    assignments = [merged[cut_id] for cut_id in sorted(merged)]
    selected_assets = [
        {
            **assignment["primary"],
            "cut_id": assignment["cut_id"],
        }
        for assignment in assignments
    ]
    data = {
        "catalog_source": deterministic._load_catalog().get("source"),
        "available_candidate_count": len(candidates),
        "asset_assignments": assignments,
        "selected_assets": selected_assets,
        "missing_requirements": [],
        "unassigned_cut_ids": [],
        "rights_check_required": False,
        "usage_scope": "agewec_submission",
        "selection_mode": "deterministic_ranker_with_llm_rationale",
        "targeted_revision_cut_id": target_cut_id,
        "requested_asset_id": requested_id,
        "rationale_fallback_used": bool(
            rationale_metadata.get("fallback")
        ),
    }
    update = deterministic._complete(
        state,
        phase,
        summary=(
            f"コードが{len(assignments)}カットへ公式素材を確定し、"
            + (
                "LLMが理由を説明"
                if not rationale_metadata.get("fallback")
                else "コード理由で継続"
            )
        ),
        data=data,
        confidence=0.98,
        warnings=warnings,
    )
    return _with_llm_metadata(
        update,
        phase,
        rationale_metadata,
    )


def director(state: WorkflowState) -> dict[str, Any]:
    storyboard = _result_data(state, "writer_storyboard")
    assets = _result_data(state, "asset_curator")
    concept = _result_data(state, "creative_director")
    if not storyboard or not assets or not concept:
        return deterministic._complete(
            state,
            "director",
            summary="上流成果物不足のため演出設計不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Concept、Storyboard、AssetSelectionが必要"],
        )
    context = _review_context(state, "director")
    target_cut_id = context.get("target_cut_id")
    if target_cut_id is not None:
        target_cut_id = int(target_cut_id)

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        cut_map = {int(cut["id"]): cut for cut in storyboard["cuts"]}
        if target_cut_id is not None and target_cut_id not in cut_map:
            raise ValueError(f"Unknown target_cut_id: {target_cut_id}")
        assignment_map = {
            int(item["cut_id"]): item
            for item in assets.get("asset_assignments", [])
        }
        if set(assignment_map) != set(cut_map):
            missing = sorted(set(cut_map) - set(assignment_map))
            raise ValueError(
                "Asset assignment is required for every cut; missing: "
                + ", ".join(map(str, missing))
            )
        new_shots: dict[int, dict[str, Any]] = {}
        invalid = []
        for direction in data["shots"]:
            cut_id = int(direction["cut_id"])
            raw_asset_id = direction["asset_id"]
            asset_id = _canonical_asset_id(raw_asset_id)
            if cut_id not in cut_map:
                invalid.append(f"unknown cut={cut_id}")
                continue
            if target_cut_id is not None and cut_id != target_cut_id:
                continue
            assignment = assignment_map[cut_id]
            choices = [assignment["primary"], *assignment["alternatives"]]
            asset_map = {item["asset_id"]: item for item in choices}
            if asset_id not in asset_map:
                invalid.append(
                    f"cut={cut_id}, asset={raw_asset_id} "
                    f"(normalized={asset_id}) is not assigned to cut"
                )
                continue
            if (
                direction.get("deviation_reason")
                and not direction["deviation_reason"].strip()
            ):
                direction["deviation_reason"] = None
            new_shots[cut_id] = {
                **cut_map[cut_id],
                "asset": asset_map[asset_id],
                "positive_prompt": direction["positive_prompt"],
                "negative_prompt": direction["negative_prompt"],
                "camera_motion": direction["camera_motion"],
                "motion_intensity": direction["motion_intensity"],
                "rationale": direction["rationale"],
                "camera_intent_alignment": direction[
                    "camera_intent_alignment"
                ],
                "deviation_reason": direction.get("deviation_reason"),
            }
        if invalid:
            raise ValueError(
                "Director returned invalid IDs/assets: " + ", ".join(invalid)
            )
        existing = (
            _result_data(state, "director").get("shots", [])
            if target_cut_id is not None
            else []
        )
        merged = {
            int(shot["id"]): shot
            for shot in existing
            if int(shot["id"]) in cut_map
        }
        merged.update(new_shots)
        if target_cut_id is not None and target_cut_id not in new_shots:
            raise ValueError(
                f"Targeted retry must return cut {target_cut_id}"
            )
        if set(merged) != set(cut_map):
            missing = sorted(set(cut_map) - set(merged))
            raise ValueError(
                "Director must return exactly one shot per cut; missing: "
                + ", ".join(map(str, missing))
            )
        shots = [merged[cut_id] for cut_id in sorted(merged)]
        return {
            "shots": shots,
            "continuity_checks": data["continuity_checks"],
            "targeted_revision_cut_id": target_cut_id,
            "technical_parameters_status": "pending_support_video_creator",
        }

    return _run_role(
        state,
        phase="director",
        upstream={
            "project_brief": _approved_project_brief(state),
            "creative_concept": concept,
            "storyboard": storyboard,
            "asset_manifest": assets,
            "camera_intent": concept.get("camera_intent", {}),
            "target_cut_id": target_cut_id,
            "existing_direction_plan": (
                _result_data(state, "director")
                if target_cut_id is not None
                else {}
            ),
            "locked_cut_rule": (
                "When target_cut_id is supplied, return only that cut. "
                "All other approved shots are locked."
            ),
        },
        summary=lambda data: (
            f"LLMが{len(data['shots'])}カットの演出指示を確定"
        ),
        fallback=deterministic.director,
        transform=transform,
    )


def image_video_production(state: WorkflowState) -> dict[str, Any]:
    director_data = _result_data(state, "director")
    if not director_data.get("shots"):
        return deterministic._complete(
            state,
            "image_video_production",
            summary="有効な演出設計がないため生成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Directorのshotsが必要"],
        )
    return deterministic.image_video_production(state)


def visual_qa(state: WorkflowState) -> dict[str, Any]:
    technical_update = deterministic.visual_qa(state)
    technical_result = technical_update["phase_results"]["visual_qa"]
    try:
        settings = _llm_settings(state)
    except Exception as exc:
        return _llm_error(state, "visual_qa", exc)
    if not settings.enabled or technical_result.get("blocking_issues"):
        return technical_update

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "checked_artifacts": len(
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
            "checks": ["ファイル存在", "ゼロバイトでないこと", "LLM metadata review"],
        }

    return _run_role(
        state,
        phase="visual_qa",
        upstream={
            "project_brief": _approved_project_brief(state),
            "storyboard": _result_data(state, "writer_storyboard"),
            "direction_plan": _result_data(state, "director"),
            "production_result": _result_data(
                state, "image_video_production"
            ),
            "artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
            "deterministic_checks": technical_result,
            "evidence_limit": (
                "No decoded video frames are supplied in this text-only pass."
            ),
        },
        summary=lambda data: (
            f"LLM Visual QA: {data['verdict']} → {data['route']}"
        ),
        fallback=lambda _: technical_update,
        transform=transform,
        warnings=["現在のLLM QAは映像フレームではなくメタデータ評価"],
    )


def post_production(state: WorkflowState) -> dict[str, Any]:
    """[LEGACY 未使用] 旧・LLMで編集計画のみ作る版（`ffmpeg_pending`）。

    本番は `pipeline_runtime.post_production`（FFmpegで実結合＝`ffmpeg_executed`）。
    互換のため残置。新しい実装はこちらに追加しないこと。
    """
    production_result = _result_data(state, "image_video_production")
    if not production_result:
        return deterministic._complete(
            state,
            "post_production",
            summary="Production成果物がないため編集計画を作成不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Image/Video Production成果物が必要"],
        )

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        output_dir = deterministic._work_path(state, "post")
        output_dir.mkdir(parents=True, exist_ok=True)
        plan_path = output_dir / "post_production_plan.json"
        payload = {
            **data,
            "implementation": "ffmpeg_pending",
            "source_artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
        }
        plan_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload["plan_path"] = str(plan_path)
        return payload

    return _run_role(
        state,
        phase="post_production",
        upstream={
            "project_brief": _approved_project_brief(state),
            "storyboard": _result_data(state, "writer_storyboard"),
            "visual_qa": _result_data(state, "visual_qa"),
            "media_artifacts": (
                state.get("phase_results", {})
                .get("image_video_production", {})
                .get("artifacts", [])
            ),
        },
        summary=lambda data: (
            f"LLMが{len(data['operations'])}工程の編集計画を作成"
        ),
        fallback=deterministic.post_production,
        transform=transform,
        warnings=["FFmpeg実行は次の実装段階"],
    )


def review_board(state: WorkflowState) -> dict[str, Any]:
    post = _result_data(state, "post_production")
    if not post:
        return deterministic._complete(
            state,
            "review_board",
            summary="Post Production成果物がないため評価不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["Post Production成果物が必要"],
        )
    return _run_role(
        state,
        phase="review_board",
        upstream={
            "project_brief": _approved_project_brief(state),
            "creative_concept": _result_data(state, "creative_director"),
            "storyboard": _result_data(state, "writer_storyboard"),
            "asset_manifest": _result_data(state, "asset_curator"),
            "visual_qa": _result_data(state, "visual_qa"),
            "post_production": post,
            "evidence_limit": (
                "Rendered final MP4 inspection is not yet available."
            ),
        },
        summary=lambda data: (
            f"LLM Review Board: {data['average']}/5、{data['verdict']}"
        ),
        fallback=deterministic.review_board,
    )


def provenance(state: WorkflowState) -> dict[str, Any]:
    return deterministic.provenance(state)
