"""Asset Curator role, shortlist, ranking, and explicit choices."""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..fallbacks import assets as deterministic
from ..llm import RoleRunner
from ..state import WorkflowState

from .common import (
    _approved_project_brief, _approved_project_value, _feedback,
    _llm_settings, _result_data, _review_context, _with_llm_metadata,
)
from .storyboard import normalize_time_of_day

def _asset_candidates(state: WorkflowState) -> list[dict[str, Any]]:
    award = _approved_project_value(state, "target_award", "夜景賞")
    target_genre = deterministic.AWARD_GENRES.get(award)
    config = state.get("config", {})
    catalog = deterministic._load_catalog(config)
    photos = catalog.get("photos", [])
    candidates = []
    for index, photo in enumerate(photos, start=1):
        title = str(photo.get("title", ""))
        genres = list(photo.get("genres", []))
        local_path = deterministic._local_asset_path(photo, config)
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


def _score_candidate_for_cut(
    cut: dict[str, Any],
    candidate: dict[str, Any],
    award: str,
) -> int:
    """Score one candidate for one cut using the shortlist's own criteria.

    ショートリスト外の素材を人間が指定したとき、その素材には
    scores_by_cut が付いていない。同じ基準で採点し直すことで、
    「なぜ低かったのか」を証跡に残せるようにする。
    """
    award_genre = deterministic.AWARD_GENRES.get(award)
    _, score = _tod_eval(
        str(cut.get("time_of_day", "unspecified")),
        candidate.get("time_of_day", "unknown_or_day"),
    )
    score += _location_score(str(cut.get("location", "")), candidate.get("areas", []))
    if award_genre and award_genre in candidate.get("genres", []):
        score += 3 if str(cut.get("visual_role", "")) in _CLIMAX_ROLES else 1
    score += _subject_score(cut, candidate.get("title", ""))
    return int(score)


def _resolve_requested_asset(
    requested_id: str,
    cut: dict[str, Any],
    ranked: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
    award: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve a human-named asset, allowing choices outside the shortlist.

    ショートリストは機械の提案であって、人間の指示を却下する根拠ではない。
    上位N件に入らなかったという理由だけで拒否せず、「実際に使えるか」
    ——ローカルに実体があり、利用条件を満たすか——だけで判断する。

    Returns:
        (採用する素材 or None, 拒否理由 or None, 警告 or None)
    """
    cut_id = int(cut["id"])
    inside = next(
        (item for item in ranked if item["asset_id"] == requested_id),
        None,
    )
    if inside is not None:
        return inside, None, None

    outside = next(
        (
            item
            for item in all_candidates
            if item["asset_id"] == requested_id
        ),
        None,
    )
    if outside is None:
        return None, (
            f"cut {cut_id}: 指定素材{requested_id}は素材カタログに存在しません"
        ), None

    # ここから先は「スコアが低い」ではなく「使えない」場合だけ拒否する。
    # 課金してから失敗するより、選定の時点で止めるほうが安い。
    local_path = str(outside.get("local_path") or "")
    if not outside.get("local_available") or not local_path:
        return None, (
            f"cut {cut_id}: 指定素材{requested_id}はローカルに未取得です"
        ), None
    if not Path(local_path).is_file():
        return None, (
            f"cut {cut_id}: 指定素材{requested_id}のローカルファイルが"
            f"見つかりません: {local_path}"
        ), None
    rights = str(outside.get("rights_status") or "")
    if rights != "approved_for_agewec_submission":
        return None, (
            f"cut {cut_id}: 指定素材{requested_id}は利用条件を満たしません"
            f"（rights_status={rights or '不明'}）"
        ), None

    # 採用する。ショートリスト由来の情報が無いので、同じ基準で補う。
    score = _score_candidate_for_cut(cut, outside, award)
    resolved = {
        **outside,
        "eligible_cut_ids": sorted(
            {
                int(value)
                for value in outside.get("eligible_cut_ids", [])
            }
            | {cut_id}
        ),
        "scores_by_cut": {
            **{
                str(key): int(value)
                for key, value in (outside.get("scores_by_cut") or {}).items()
            },
            str(cut_id): score,
        },
        "outside_shortlist": True,
    }
    return resolved, None, (
        f"cut {cut_id}: {requested_id}は機械の候補外（スコア{score}）でしたが、"
        "人間の明示指定により採用しました"
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
        # 候補外を指定した場合は、後から「なぜこの写真か」を追えるよう明記する
        outside = (
            "機械の候補（上位N件）には入っていなかったが、"
            if candidate.get("outside_shortlist")
            else ""
        )
        return (
            f"{outside}人間の明示指定により"
            f"{candidate.get('title', '')}を採用。"
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


# 「Cut1:Asset-009, Cut2:Asset-076, ...」のようなカット単位の一括指定。
# 全角コロン・空白・アンダースコアの揺れを吸収する。
_CUT_ASSET_PATTERN = re.compile(
    r"cut\s*(\d+)\s*[:：]?\s*asset[-\s_]*(\d+)",
    flags=re.IGNORECASE,
)


def _requested_asset_map(feedback: str) -> dict[int, str]:
    """Parse a per-cut asset assignment written by a human.

    `_requested_asset_id` は re.search なので最初の1件しか拾えない。
    8カット分を書いても7件が黙って捨てられていたため、カット番号と
    素材IDの対を全て取り出せるようにする。

    同じカットが複数回書かれた場合は、後に書かれたほうを採用する
    （書き直しは後から上書きするのが自然なため）。
    """
    assignments: dict[int, str] = {}
    for cut_text, asset_text in _CUT_ASSET_PATTERN.findall(feedback):
        cut_id = int(cut_text)
        if cut_id < 1:
            continue
        assignments[cut_id] = _canonical_asset_id(f"asset-{asset_text}")
    return assignments


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
    # ショートリストは「機械の提案」。人間の明示指定はここに縛られないため、
    # 絞り込み前の全件も保持しておく（_resolve_requested_asset が使う）。
    all_candidates = _asset_candidates(state)
    award = _approved_project_value(state, "target_award", "夜景賞")
    per_cut = int(
        state.get("config", {}).get("assets", {}).get("shortlist_per_cut", 8)
    )
    candidates = _shortlist_candidates(
        storyboard["cuts"], all_candidates, award, per_cut
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

    feedback = _feedback(state, phase)
    # 一括指定が一部のカットだけを指す場合、指定外のカットは前回の選定を残す。
    # ここで前回結果を読まないと、指定外のカットが未割当のまま落ちる。
    _bulk_requested = bool(_requested_asset_map(feedback))
    existing_items = (
        _result_data(state, phase).get("asset_assignments", [])
        if (target_cut_id is not None or _bulk_requested)
        else []
    )
    merged = {
        int(item["cut_id"]): copy.deepcopy(item)
        for item in existing_items
        if int(item["cut_id"]) in valid_cut_ids
    }
    # 「Cut1:Asset-009, Cut2:Asset-076, ...」形式の一括指定を先に見る。
    # カット番号が文中にあるため、対象カットIDの指定は不要。
    requested_map = _requested_asset_map(feedback)
    unknown_cut_ids = sorted(set(requested_map) - valid_cut_ids)
    if unknown_cut_ids:
        return deterministic._complete(
            state,
            phase,
            summary="存在しないカット番号が指定されています",
            data={
                "selection_mode": "deterministic_ranker",
                "requested_asset_map": {
                    str(key): value for key, value in requested_map.items()
                },
            },
            status="error",
            confidence=0.0,
            blocking_issues=[
                "絵コンテにないカット番号: "
                + ", ".join(map(str, unknown_cut_ids))
                + f"（有効なカット: {', '.join(map(str, sorted(valid_cut_ids)))}）"
            ],
        )
    requested_id = None if requested_map else _requested_asset_id(feedback)
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
    if requested_map:
        # 指定されたカットは必ず割り当て直す。あわせて、まだ選定されていない
        # カット（初回実行など）も埋める。既に選定済みで指定外のカットは
        # 触らない——黙って別の素材に変わるのを防ぐため。
        needed = (valid_cut_ids - set(merged)) | set(requested_map)
        if target_cut_id is not None:
            needed.add(target_cut_id)
        selection_cut_ids = sorted(needed)
    else:
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
        cut_requested_id = requested_map.get(cut_id) or requested_id
        if cut_requested_id:
            requested, rejection, warning = _resolve_requested_asset(
                cut_requested_id,
                cut,
                ranked,
                all_candidates,
                award,
            )
            if requested is None:
                blocking.append(str(rejection))
                continue
            if warning:
                warnings.append(warning)
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
        "catalog_source": deterministic._load_catalog(
            state.get("config", {})
        ).get("source"),
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
        # 人間がカット単位で名指しした内容。後から誰が決めたかを追えるよう残す。
        "requested_asset_map": {
            str(key): value for key, value in sorted(requested_map.items())
        },
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
