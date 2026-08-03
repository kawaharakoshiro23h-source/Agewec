"""Human-readable terminal presentation for review gates.

The full structured payload is kept in the gate snapshot JSON.  This module
intentionally presents only the information a reviewer needs to understand
and approve the result.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_STATUS_LABELS = {
    "success": "成功",
    "ok": "成功",
    "completed": "成功",
    "warning": "要確認",
    "error": "失敗",
    "failed": "失敗",
    "blocked": "停止",
    "skipped": "スキップ",
}

_FIELD_LABELS = {
    "objective": "目的",
    "audience": "対象者",
    "deliverable": "完成物",
    "target_award": "対象賞",
    "target_duration_seconds": "目標尺",
    "constraints": "制約",
    "success_criteria": "成功基準",
    "title": "コンセプト名",
    "logline": "一言企画",
    "tone": "雰囲気",
    "cuts": "カット構成",
    "total_seconds": "合計尺",
    "asset_assignments": "素材選定",
    "shots": "演出設計",
    "requests": "動画生成設定",
    "verdict": "判定",
    "issues": "問題点",
}

_INTERNAL_CHANGE_FIELDS = {
    "targeted_revision_cut_id",
    "request_count",
    "request_contract",
    "frame_rule",
}

_PHASE_DESCRIPTIONS = {
    "executive_producer": "動画の目的・対象者・尺・成功条件を決めました。",
    "creative_director": "作品全体のコンセプト・雰囲気・映像表現を決めました。",
    "writer_storyboard": "動画をカットに分け、各場面とナレーションを構成しました。",
    "asset_curator": "各カットに使用する元画像と利用条件を確認しました。",
    "director": "各カットのカメラワークと動画生成指示を設計しました。",
    "support_video_creator": "外部サービスへ渡す動画生成設定と費用をまとめました。",
    "image_video_production": "元画像からカット動画を生成しました。",
    "cut_visual_qa": "生成したカット動画が使用可能か確認しました。",
    "visual_qa": "全カットを連結工程へ進められる状態か確認しました。",
    "post_production": "カットを連結し、最終動画の技術検査を行いました。",
    "review_board": "完成動画を提出してよいか総合評価しました。",
    "provenance": "制作記録と提出用ファイルをまとめました。",
    "final_submission": "最終動画と制作記録を提出してよいか確認します。",
}


def _text(value: Any, limit: int = 320) -> str:
    if value in (None, ""):
        return "—"
    rendered = " ".join(str(value).split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1].rstrip() + "…"


def _basename(value: Any) -> str:
    if not value:
        return "—"
    name = Path(str(value)).name
    return re.sub(r"^asset-\d+_", "", name)


def _list_lines(label: str, values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        return []
    lines = [f"  {label}:"]
    lines.extend(f"    - {_text(value)}" for value in values)
    return lines


def _executive(data: dict[str, Any]) -> list[str]:
    lines = [
        f"  目的: {_text(data.get('objective'))}",
        f"  対象者: {_text(data.get('audience'))}",
        f"  完成物: {_text(data.get('deliverable'))}",
        f"  目標尺: {_text(data.get('target_duration_seconds'))}秒",
        f"  対象賞: {_text(data.get('target_award'))}",
    ]
    lines += _list_lines("制約", data.get("constraints"))
    lines += _list_lines("成功基準", data.get("success_criteria"))
    return lines


def _creative(data: dict[str, Any]) -> list[str]:
    lines = [
        f"  コンセプト: {_text(data.get('title'))}",
        f"  一言企画: {_text(data.get('logline'))}",
    ]
    tone = data.get("tone")
    if isinstance(tone, list) and tone:
        lines.append(f"  雰囲気: {', '.join(map(str, tone))}")
    camera = data.get("camera_intent") or {}
    if isinstance(camera, dict):
        lines.extend(
            line
            for line in (
                f"  視聴体験: {_text(camera.get('viewer_experience'))}",
                f"  展開: {_text(camera.get('energy_curve'))}",
            )
            if not line.endswith("—")
        )
    if data.get("audio_direction"):
        lines.append(f"  音の方針: {_text(data['audio_direction'])}")
    return lines


def _storyboard(data: dict[str, Any]) -> list[str]:
    cuts = data.get("cuts") or []
    lines = [
        f"  全体: {len(cuts)}カット / {_text(data.get('total_seconds'))}秒"
    ]
    for cut in cuts:
        lines.extend(
            [
                "",
                (
                    f"  Cut {cut.get('id')}: {_text(cut.get('name'), 100)} "
                    f"（{_text(cut.get('seconds'))}秒）"
                ),
                f"    映像: {_text(cut.get('scene'))}",
                (
                    f"    場所・被写体: {_text(cut.get('location'))} / "
                    f"{_text(cut.get('subject'))}"
                ),
                f"    ナレーション: {_text(cut.get('narration'))}",
            ]
        )
    cut_limit = data.get("cut_limit") or {}
    if cut_limit.get("applied"):
        lines.extend(
            [
                "",
                (
                    "  カット数制限を適用: "
                    f"{cut_limit.get('original_cut_count')} → "
                    f"{cut_limit.get('final_cut_count')}カット"
                ),
            ]
        )
    return lines


def _assets(data: dict[str, Any]) -> list[str]:
    assignments = data.get("asset_assignments") or []
    lines = [f"  選定結果: {len(assignments)}カット分の素材を確定"]
    for item in assignments:
        primary = item.get("primary") or {}
        alternatives = item.get("alternatives") or []
        rights = primary.get("rights_status")
        rights_label = (
            "利用条件を確認済み"
            if rights == "approved_for_agewec_submission"
            else _text(rights)
        )
        lines.extend(
            [
                "",
                f"  Cut {item.get('cut_id')}: {_text(primary.get('title'), 120)}",
                f"    エリア: {_text(', '.join(primary.get('areas') or []))}",
                f"    選定理由: {_text(primary.get('llm_rationale') or primary.get('selection_reason'))}",
                f"    権利確認: {rights_label}",
            ]
        )
        names = [alt.get("title") for alt in alternatives if alt.get("title")]
        if names:
            lines.append(f"    代替候補: {', '.join(names)}")
    return lines


def _director(data: dict[str, Any]) -> list[str]:
    shots = data.get("shots") or []
    lines = [f"  演出結果: {len(shots)}カット分を設計"]
    for shot in shots:
        asset = shot.get("asset") or {}
        lines.extend(
            [
                "",
                (
                    f"  Cut {shot.get('id')}: {_text(shot.get('name'), 100)} "
                    f"（{_text(shot.get('seconds'))}秒）"
                ),
                f"    映像: {_text(shot.get('scene'))}",
                f"    使用素材: {_text(asset.get('title'))}",
                f"    カメラ: {_text(shot.get('camera_motion'))}",
                f"    生成指示: {_text(shot.get('positive_prompt'))}",
                f"    ナレーション: {_text(shot.get('narration'))}",
            ]
        )
    return lines


def _support_video(data: dict[str, Any]) -> list[str]:
    requests = data.get("requests") or []
    cost = data.get("cost_estimate") or {}
    backend = str(data.get("backend") or "—")
    backend_label = "Runway API" if backend == "runway" else backend
    lines = [
        f"  生成先: {backend_label}",
        f"  使用モデル: {_text(cost.get('model'))}",
        f"  対象: {len(requests)}カット",
    ]
    if data.get("profile_name"):
        lines.insert(2, f"  品質設定: {_text(data.get('profile_name'))}")
    if cost.get("total_usd") is not None:
        lines.append(f"  概算費用: US${float(cost['total_usd']):.2f}")
    for request in requests:
        lines.extend(
            [
                "",
                (
                    f"  Cut {request.get('cut_id')}: "
                    f"{_text(request.get('requested_seconds'))}秒"
                ),
                f"    元画像: {_basename(request.get('image_path'))}",
                f"    カメラ: {_text(request.get('camera_motion'))}",
                f"    生成指示: {_text(request.get('positive_prompt'))}",
            ]
        )
    return lines


def _qa_reason(data: dict[str, Any]) -> str:
    generation_error = data.get("generation_error") or {}
    if generation_error:
        error_type = _text(
            generation_error.get("exception_type") or "GenerationError"
        )
        message = _text(
            generation_error.get("message") or "詳細なし"
        )
        return f"動画生成で{error_type}が発生しました: {message}"
    issues = data.get("issues") or []
    issue_codes = {item.get("code") for item in issues if isinstance(item, dict)}
    if "MEDIA_TECHNICAL_ERROR" in issue_codes and not data.get("artifact_path"):
        return "動画生成結果のファイルが作成されなかったため、映像を確認できませんでした。"
    if issues:
        first = issues[0]
        if isinstance(first, dict):
            description = str(first.get("description") or "")
        else:
            description = str(first)
        return description.removeprefix("MediaToolError: ") or "問題が検出されました。"
    return "問題は検出されませんでした。"


def _cut_visual_qa(data: dict[str, Any]) -> list[str]:
    verdict = data.get("verdict")
    verdict_label = {
        "pass": "合格",
        "approve": "合格",
        "revise": "再生成が必要",
        "fail": "不合格",
    }.get(str(verdict), _text(verdict))
    artifact = data.get("artifact_path")
    lines = [
        f"  対象: Cut {data.get('cut_id')}（{data.get('attempt', 1)}回目）",
        f"  判定: {verdict_label}",
        f"  映像確認: {'実施済み' if artifact else '未実施'}",
        f"  理由: {_qa_reason(data)}",
        f"  元画像: {_basename(data.get('source_image'))}",
    ]
    if artifact:
        lines.append(f"  生成動画: {artifact}")
    visual = data.get("visual_evaluation") or {}
    if visual.get("status") == "not_evaluated" and artifact:
        lines.append("  AIによる内容確認: 未実施（VLM未設定）")
    route = data.get("recommended_route")
    recommendation = {
        "image_video_production": "同じ条件で動画をもう一度生成する",
        "support_video_creator": "動画の生成設定を見直す",
        "director": "演出・生成指示を見直す",
        "asset_curator": "元画像を変更する",
    }.get(str(route))
    if recommendation:
        lines.extend(["", f"  推奨対応: {recommendation}"])
    return lines


def _production(data: dict[str, Any]) -> list[str]:
    artifact = data.get("artifact") or {}
    succeeded = bool(artifact and artifact.get("path"))
    backend = data.get("backend")
    backend_label = "Runway API" if backend == "runway" else _text(backend)
    lines = [
        f"  対象: Cut {data.get('cut_id')}（{data.get('attempt', 1)}回目）",
        f"  生成先: {backend_label}",
        f"  生成結果: {'成功' if succeeded else '失敗'}",
    ]
    if succeeded:
        lines.append(f"  生成動画: {artifact.get('path')}")
    else:
        lines.append("  状態: 動画ファイルを取得できませんでした。")
    return lines


def _sequence_qa(data: dict[str, Any]) -> list[str]:
    verdict = "合格" if data.get("verdict") == "pass" else "修正が必要"
    affected = data.get("affected_cut_ids") or []
    lines = [f"  判定: {verdict}"]
    if affected:
        lines.append(f"  対象カット: {', '.join(map(str, affected))}")
    issues = data.get("issues") or []
    if issues:
        lines.append("  問題:")
        for issue in issues:
            description = issue.get("description") if isinstance(issue, dict) else issue
            lines.append(f"    - {_text(description)}")
    else:
        lines.append("  確認結果: 全カットが承認済みで、予定尺とも一致しています。")
    return lines


def _post_production(data: dict[str, Any]) -> list[str]:
    technical = data.get("technical_qa") or {}
    lines = [
        f"  最終動画: {_text(data.get('output_path'))}",
        (
            "  技術検査: "
            + ("合格" if technical.get("status") == "pass" else "要確認")
        ),
    ]
    if technical:
        resolution = "—"
        if technical.get("width") and technical.get("height"):
            resolution = f"{technical['width']}x{technical['height']}"
        lines.extend(
            [
                f"  尺: {_text(technical.get('duration_seconds'))}秒",
                f"  解像度: {resolution}",
                f"  フレームレート: {_text(technical.get('fps'))}fps",
            ]
        )
    lines += _list_lines("問題", data.get("issues"))
    return lines


def _review_board(data: dict[str, Any]) -> list[str]:
    if data.get("mode") == "human_only":
        return [
            "  AIによる総合評価: 省略",
            "  最終判断: 人間による確認が必要",
            f"  理由: {_text(data.get('reason'))}",
        ]
    verdict = "合格" if data.get("verdict") == "pass" else "修正が必要"
    lines = [f"  判定: {verdict}"]
    if data.get("average") is not None:
        lines.append(f"  平均評価: {data['average']} / 5")
    lines += _list_lines("推奨事項", data.get("recommendations"))
    return lines


def _provenance(data: dict[str, Any]) -> list[str]:
    return [
        f"  提出フォルダ: {_text(data.get('package_dir'))}",
        f"  最終動画: {_text(data.get('final_video'))}",
        f"  制作レポート: {_text(data.get('process_report'))}",
        f"  証跡一覧: {_text(data.get('manifest'))}",
    ]


def _generic(data: dict[str, Any], summary: Any) -> list[str]:
    if not data:
        return [f"  内容: {_text(summary)}"]
    preferred = (
        ("判定", "verdict"),
        ("最終動画", "output_path"),
        ("提出フォルダ", "package_dir"),
        ("平均評価", "average"),
        ("実装結果", "implementation"),
    )
    lines = [f"  内容: {_text(summary)}"]
    for label, key in preferred:
        if data.get(key) not in (None, "", [], {}):
            lines.append(f"  {label}: {_text(data[key])}")
    return lines


_PHASE_RENDERERS = {
    "executive_producer": _executive,
    "creative_director": _creative,
    "writer_storyboard": _storyboard,
    "asset_curator": _assets,
    "director": _director,
    "support_video_creator": _support_video,
    "image_video_production": _production,
    "cut_visual_qa": _cut_visual_qa,
    "visual_qa": _sequence_qa,
    "post_production": _post_production,
    "review_board": _review_board,
    "final_submission": _review_board,
    "provenance": _provenance,
}


def review_summary_lines(payload: dict[str, Any]) -> list[str]:
    """Return the concise, human-facing presentation for a gate payload."""
    phase = str(payload.get("phase") or "")
    data = payload.get("data") or {}
    status = str(payload.get("status") or "unknown")
    description = _PHASE_DESCRIPTIONS.get(phase)
    lines = [
        f"  結果: {_STATUS_LABELS.get(status, status)}",
        f"  この工程: {description or _text(payload.get('summary'))}",
        "  --- 確認する内容 ---",
    ]
    renderer = _PHASE_RENDERERS.get(phase)
    if renderer:
        lines.extend(renderer(data))
    else:
        lines.extend(_generic(data, payload.get("summary")))
    if phase == "final_submission" and payload.get("final_video"):
        lines.append(f"  提出する動画: {payload['final_video']}")
    return lines


def changed_field_labels(previous: Any, current: dict[str, Any]) -> list[str]:
    """Return reviewer-friendly labels for changed top-level fields."""
    if not isinstance(previous, dict):
        return []
    changed = sorted(
        key
        for key in set(previous) | set(current)
        if key not in _INTERNAL_CHANGE_FIELDS
        and previous.get(key) != current.get(key)
    )
    return [_FIELD_LABELS.get(key, key) for key in changed]


def feedback_status_label(status: Any) -> str:
    return {
        "not_provided": "フィードバックなし",
        "delivered_to_llm_pending_human_verification": (
            "LLMへ送信済み。修正結果を人間が確認してください"
        ),
        "received_not_applied_by_deterministic_node": (
            "受信済み。ただしこの工程では自動反映できません"
        ),
    }.get(str(status), _text(status))


def feedback_source_label(origin: Any) -> str:
    return {
        "human": "あなたの修正指示",
        "ai_qa": "QAによる自動修正提案",
        "system": "システムからの修正情報",
    }.get(str(origin), "前工程からの修正情報")
