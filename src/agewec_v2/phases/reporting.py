"""Human-readable and machine-readable process report rendering."""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from ..fallbacks import common as deterministic
from .. import timing
from ..paths import runtime_paths
from ..state import WorkflowState

from .common import _result_data

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decision_log(state: WorkflowState) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for event in state.get("events", []):
        decisions.append(
            {
                "timestamp": event.get("t"),
                "run_id": state.get("run_id"),
                "phase": event.get("phase"),
                "cut_id": event.get("cut_id"),
                "actor": (
                    "human"
                    if event.get("decided_by") == "human"
                    else "system"
                ),
                "action": event.get("type"),
                "decision": event.get("action") or event.get("summary"),
                "rationale": "",
                "evidence_refs": [],
            }
        )
    for review in state.get("reviews", []):
        decisions.append(
            {
                "timestamp": review.get("t"),
                "run_id": state.get("run_id"),
                "phase": review.get("phase"),
                "cut_id": review.get("target_cut_id"),
                "actor": review.get("decided_by"),
                "action": review.get("action"),
                "decision": review.get("feedback") or review.get("action"),
                "rationale": review.get("correction_type", ""),
                "evidence_refs": [],
            }
        )
    return decisions


_PHASE_PRESENTATION: tuple[dict[str, Any], ...] = (
    {
        "id": "executive_producer",
        "number": "01",
        "title": "Executive Producer（統括プロデューサー：制作要件の定義）",
        "kind": "AI（LLM）",
        "purpose": "制作依頼を、全工程が共有する目的・制約・成功基準へ変換する。",
        "input_source": "Project設定（人間が最初に指定）",
        "inputs": [
            "theme: 制作テーマ",
            "target_award: 狙う部門・評価軸",
            "target_duration_seconds: 最終目標尺",
        ],
        "process": "対象視聴者、納品物、制約、成功基準を定義する。",
        "output": "ProjectBrief JSON",
        "next": "Creative Director（コンセプト設計）",
    },
    {
        "id": "creative_director",
        "number": "02",
        "title": "Creative Director（クリエイティブディレクター：コンセプト設計）",
        "kind": "AI（LLM）",
        "purpose": "企画全体のコンセプト、色、トーン、カメラ意図を統一する。",
        "input_source": "Project設定 + ProjectBrief",
        "inputs": [
            "objective / audience / constraints",
            "success_criteria",
            "target_award / target_duration_seconds",
        ],
        "process": "作品タイトル、訴求、視覚言語、音響方針、全体演出意図を策定する。",
        "output": "CreativeConcept JSON",
        "next": "Writer / Storyboard（台本・絵コンテ）",
    },
    {
        "id": "writer_storyboard",
        "number": "03",
        "title": "Writer / Storyboard（脚本・絵コンテ：台本とカット構成）",
        "kind": "AI＋コード",
        "purpose": "コンセプトを、指定尺に収まる具体的なカット列へ分解する。",
        "input_source": "ProjectBrief + CreativeConcept",
        "inputs": [
            "作品コンセプトと成功基準",
            "目標尺",
            "カメラ意図・トーン",
        ],
        "process": "各カットの場面、秒数、ナレーション、時間帯、場所、被写体を構成し、コードで尺を補正する。",
        "output": "Storyboard JSON（cuts[]）",
        "next": "Asset Curator（素材選定）",
    },
    {
        "id": "asset_curator",
        "number": "04",
        "title": "Asset Curator（素材キュレーター：公式素材の選定）",
        "kind": "コード＋AI",
        "purpose": "各カットに、実在するAGEWEC公式写真を最低1枚割り当てる。",
        "input_source": "Storyboard + ローカル素材カタログ",
        "inputs": [
            "cut id / time_of_day / location / subject / visual_role",
            "素材ID、ジャンル、地域、時間帯、ローカルパス",
        ],
        "process": "コードが適合度を採点して写真を確定し、LLMは選定理由だけを説明する。",
        "output": "AssetManifest JSON（primary + alternatives）",
        "next": "Director（演出設計）",
    },
    {
        "id": "director",
        "number": "05",
        "title": "Director（監督：カット別の演出とプロンプト設計）",
        "kind": "AI（LLM）",
        "purpose": "各カットと選定写真を、動画生成に必要な個別演出へ変換する。",
        "input_source": "CreativeConcept + Storyboard + AssetManifest",
        "inputs": [
            "カット内容と秒数",
            "選定済みasset_id",
            "全体のカメラ意図・連続性ルール",
        ],
        "process": "positive/negative prompt、カメラ移動、動きの強度、演出根拠を作る。",
        "output": "DirectionPlan JSON（shots[]）",
        "next": "Support Video Creator（生成条件の変換）",
    },
    {
        "id": "support_video_creator",
        "number": "05.5",
        "title": "Support Video Creator（生成条件の変換：秒数→技術パラメータ）",
        "kind": "コード（自動）",
        "purpose": "演出指示を、ComfyUIが実行できる技術パラメータへ安全に変換する。",
        "input_source": "DirectionPlan + Production設定",
        "inputs": [
            "画像パスと生成Prompt",
            "Storyboard秒数",
            "解像度、FPS、steps、モデル制約",
        ],
        "process": "秒数をLTX互換フレーム数へ変換し、seedや出力設定を確定する。",
        "output": "ProductionRequest JSON（カット別）",
        "next": "Image / Video Production（映像生成）",
    },
    {
        "id": "image_video_production",
        "number": "06",
        "title": "Image / Video Production（映像生成：実写を起点にAIで動画化）",
        "kind": "生成AI（動画）",
        "purpose": "確定済み画像とPromptから、カット単位の実MP4を生成する。",
        "input_source": "ProductionRequest",
        "inputs": [
            "入力画像",
            "positive/negative prompt",
            "frames / fps / width / height / steps / seed",
        ],
        "process": "ComfyUI APIへ投入し、完了を待って生成動画と実行情報を保存する。",
        "output": "MediaArtifact（MP4 + generation metadata）",
        "next": "Cut Visual QA（カット品質検査）",
    },
    {
        "id": "cut_visual_qa",
        "number": "07A",
        "title": "Cut Visual QA（カット品質検査：尺・破綻の確認と差し戻し）",
        "kind": "コード＋人間確認",
        "purpose": "生成直後の各カットを検査し、問題の種類に応じて必要な工程だけへ戻す。",
        "input_source": "ProductionRequest + 生成MP4",
        "inputs": [
            "要求尺・解像度・FPS",
            "生成動画",
            "代表フレーム",
        ],
        "process": "デコード、尺、解像度を検査し、passまたは修正先を判定する。",
        "output": "CutQAResult JSON",
        "next": "合格→次カット ／ 不合格→Director（演出修正）・Asset Curator（素材変更）・Support Video Creator（条件変更）",
    },
    {
        "id": "visual_qa",
        "number": "07B",
        "title": "Sequence Readiness QA（全体整合検査：編集へ進めるかの判定）",
        "kind": "コード／AI",
        "purpose": "全カットが揃い、最終編集へ進める状態かを確認する。",
        "input_source": "全CutQAResult + 全生成Artifact",
        "inputs": [
            "承認済みカットID",
            "失敗カットID",
            "技術QA結果",
        ],
        "process": "欠落、不整合、未承認カットを検査して次の経路を決定する。",
        "output": "VisualQAResult JSON",
        "next": "Post Production（編集・仕上げ）",
    },
    {
        "id": "post_production",
        "number": "08",
        "title": "Post Production（編集・仕上げ：結合と最終尺の調整）",
        "kind": "コード（FFmpeg）",
        "purpose": "承認済みカットを正規化・結合し、指定尺の最終MP4にする。",
        "input_source": "Storyboard + 承認済みMediaArtifact",
        "inputs": [
            "カット順・目標秒数",
            "各MP4",
            "最終解像度・FPS",
        ],
        "process": "各カットをトリム・正規化して結合し、最終動画を再検査する。",
        "output": "final_video.mp4 + EditManifest + TechnicalReport",
        "next": "Review Board（審査会）",
    },
    {
        "id": "review_board",
        "number": "09",
        "title": "Review Board（審査会：提出水準に達したかの総合評価）",
        "kind": "AI／人間",
        "purpose": "最終動画と制作要件を採点し、提出可否または修正を判断する。",
        "input_source": "最終MP4 + 技術QA + 上流成果物",
        "inputs": [
            "コンセプト・Storyboard・素材証跡",
            "最終Technical QA",
            "評価rubric",
        ],
        "process": "AI採点または人間確認を行い、pass/reviseを返す。",
        "output": "ReviewBoardResult JSON",
        "next": "Final Submission Review（最終提出承認）",
    },
    {
        "id": "final_submission",
        "number": "H3",
        "title": "Final Submission Review（最終提出承認：人間による可否判断）",
        "kind": "人間の承認",
        "purpose": "提出直前に最終動画と未解決事項を確認し、公開を承認する。",
        "input_source": "ReviewBoardResult + final_video.mp4",
        "inputs": [
            "最終動画",
            "最終技術QA",
            "警告・Review Board結果",
        ],
        "process": "approve / retry_with_feedback / abortを選択する。",
        "output": "ReviewDecision",
        "next": "Provenance & Submission Package（証跡・提出物）",
    },
    {
        "id": "provenance",
        "number": "10",
        "title": "Provenance & Submission Package（証跡・提出物：制作過程の記録と提出パッケージ）",
        "kind": "コード（自動）",
        "purpose": "動画と全判断記録を、第三者が追跡できる提出Packageへまとめる。",
        "input_source": "全phase_results + reviews + events + artifacts",
        "inputs": [
            "各工程の構造化出力",
            "人間・自動承認履歴",
            "動画・QA・編集成果物",
        ],
        "process": "証跡をサニタイズし、レポート、JSON、ハッシュManifestを生成する。",
        "output": "Submission Package（HTML / JSON / JSONL / MP4）",
        "next": "提出完了",
    },
)


def _compact_text(value: Any, limit: int = 420) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _phase_actual_items(
    phase: str,
    result: dict[str, Any],
    state: WorkflowState,
) -> list[tuple[str, Any]]:
    data = result.get("data", {})
    if phase == "executive_producer":
        return [
            ("目的", data.get("objective")),
            ("対象", data.get("audience")),
            ("狙う賞", data.get("target_award")),
            ("目標尺", f"{data.get('target_duration_seconds')}秒"),
            ("成功基準", data.get("success_criteria", [])),
        ]
    if phase == "creative_director":
        return [
            ("コンセプト", data.get("title")),
            ("一行企画", data.get("logline")),
            ("トーン", data.get("tone", [])),
            ("カメラ意図", data.get("camera_intent", {})),
        ]
    if phase == "writer_storyboard":
        cuts = [
            (
                f"Cut {cut.get('id')}: {cut.get('name')} / "
                f"{cut.get('seconds')}秒 / {cut.get('time_of_day')} / "
                f"{cut.get('location')} — {cut.get('scene')}"
            )
            for cut in data.get("cuts", [])
        ]
        return [
            ("合計尺", f"{data.get('total_seconds')}秒"),
            ("カット構成", cuts),
            ("尺補正", data.get("duration_adjustment", {})),
        ]
    if phase == "asset_curator":
        assignments = []
        for item in data.get("asset_assignments", []):
            primary = item.get("primary", {})
            assignments.append(
                (
                    f"Cut {item.get('cut_id')}: "
                    f"{primary.get('asset_id')} {primary.get('title')} / "
                    f"コード根拠: {primary.get('selection_reason', '')} / "
                    f"LLM説明: {primary.get('llm_rationale', '')}"
                )
            )
        return [
            ("選定方式", data.get("selection_mode")),
            ("確定素材", assignments),
        ]
    if phase == "director":
        shots = [
            (
                f"Cut {shot.get('id')}: "
                + (
                    "mode=text_to_video（元画像なし）"
                    if str(shot.get("generation_mode") or "")
                    == "text_to_video"
                    else f"asset={(shot.get('asset') or {}).get('asset_id')}"
                )
                + f" / model={shot.get('model') or 'default'}"
                + f" / camera={shot.get('camera_motion')} / "
                f"prompt={_compact_text(shot.get('positive_prompt'), 260)} / "
                f"根拠={shot.get('rationale', '')}"
            )
            for shot in data.get("shots", [])
        ]
        return [
            ("カット別演出", shots),
            ("連続性確認", data.get("continuity_checks", [])),
        ]
    if phase == "support_video_creator":
        requests = state.get("production_requests", {})
        return [
            (
                "生成Request",
                [
                    f"Cut {item.get('cut_id')}: {_request_summary(item)}"
                    for item in requests.values()
                ],
            )
        ]
    if phase == "image_video_production":
        artifacts = state.get("production_artifacts", {})
        return [
            ("生成済みカット", sorted(map(int, artifacts.keys()))),
            (
                "生成動画",
                [
                    f"Cut {key}: {value.get('path')}"
                    for key, value in sorted(artifacts.items())
                ],
            ),
        ]
    if phase == "cut_visual_qa":
        qa_results = state.get("cut_qa_results", {})
        return [
            (
                "カット別QA",
                [
                    (
                        f"Cut {key}: {value.get('verdict')} / "
                        f"{value.get('issue_class')}"
                    )
                    for key, value in sorted(qa_results.items())
                ],
            ),
            ("承認済みカット", state.get("approved_cut_ids", [])),
        ]
    if phase == "visual_qa":
        return [
            ("判定", data.get("verdict") or result.get("status")),
            ("問題", data.get("issues", [])),
            ("次の経路", data.get("route") or data.get("recommended_route")),
        ]
    if phase == "post_production":
        technical = data.get("technical_qa", {})
        return [
            ("実装", data.get("implementation")),
            ("最終動画", _portable_path(data.get("output_path") or "—")),
            (
                "Technical QA",
                {
                    "status": technical.get("status"),
                    "duration_seconds": technical.get("duration_seconds"),
                    "resolution": (
                        f"{technical.get('width')}x{technical.get('height')}"
                    ),
                    "fps": technical.get("fps"),
                },
            ),
        ]
    if phase == "review_board":
        return [
            ("モード", data.get("mode", "ai")),
            ("判定", data.get("verdict")),
            ("平均点", data.get("average")),
            ("推奨事項", data.get("recommendations", [])),
        ]
    if phase == "provenance":
        return [
            ("提出Package", data.get("package_dir")),
            ("最終動画", data.get("final_video")),
            ("Process Report", data.get("process_report")),
            ("Manifest", data.get("manifest")),
        ]
    return [("生成結果", result.get("summary", ""))]


def _format_markdown_value(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(_compact_text(item) for item in value) or "—"
    if isinstance(value, dict):
        return "; ".join(
            f"{key}={_compact_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ) or "—"
    return _compact_text(value) or "—"


def _feedback_actual_items(result: dict[str, Any]) -> list[tuple[str, Any]]:
    feedback = str(result.get("feedback_received") or "")
    if not feedback:
        return []
    previous = result.get("previous_data")
    current = result.get("data", {})
    changed = []
    if isinstance(previous, dict) and isinstance(current, dict):
        changed = sorted(
            key
            for key in set(previous) | set(current)
            if previous.get(key) != current.get(key)
        )
    feedback_label = {
        "human": "人間フィードバック",
        "ai_qa": "QAによる自動修正提案",
        "system": "システム修正情報",
    }.get(str(result.get("feedback_origin")), "前工程からの修正情報")
    return [
        (feedback_label, feedback),
        ("フィードバック状態", result.get("feedback_status")),
        (
            "反映確認用差分",
            changed or result.get("feedback_application_evidence") or "変更なし",
        ),
    ]


def _render_html_value(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "<span class='muted'>—</span>"
        return "<ul>" + "".join(
            f"<li>{html.escape(_compact_text(item))}</li>"
            for item in value
        ) + "</ul>"
    if isinstance(value, dict):
        if not value:
            return "<span class='muted'>—</span>"
        return "<dl>" + "".join(
            f"<dt>{html.escape(str(key))}</dt>"
            f"<dd>{html.escape(_compact_text(item))}</dd>"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ) + "</dl>"
    return f"<span>{html.escape(_compact_text(value)) or '—'}</span>"


def _process_markdown(state: WorkflowState, video_name: str) -> str:
    lines = [
        "# AGEWEC Production Process Report",
        "",
        f"- Run ID: `{state.get('run_id')}`",
        f"- Final video: `{video_name}`",
        f"- Target duration: "
        f"{state.get('project', {}).get('target_duration_seconds')} seconds",
        "",
        "## 全体ワークフロー",
        "",
        " → ".join(
            f"{item['number']} {item['title']}"
            for item in _PHASE_PRESENTATION
        ),
        "",
        "各工程の後には設定されたReview Gateがあり、承認、対象工程の再実行、"
        "中止を選べます。Cut QAは問題種別に応じて生成、演出、素材選定へ戻ります。",
        "",
        "## ノードごとの入出力と実行結果",
        "",
    ]
    phase_results = state.get("phase_results", {})
    reviews = state.get("reviews", [])
    for static_guide in _PHASE_PRESENTATION:
        # 実際に使ったバックエンドに合わせて説明文を差し替える
        guide = _guide_for_backend(static_guide, state)
        phase = guide["id"]
        result = phase_results.get(phase, {})
        related_reviews = [
            review
            for review in reviews
            if review.get("phase") == phase
            or review.get("source_phase") == phase
        ]
        lines.extend(
            [
                f"### {guide['number']} {guide['title']}",
                "",
                f"- 種別: `{guide['kind']}`",
                f"- 目的: {guide['purpose']}",
                f"- 入力元: {guide['input_source']}",
                f"- 入力情報: {'; '.join(guide['inputs'])}",
                f"- 処理: {guide['process']}",
                f"- 出力形式: `{guide['output']}`",
                f"- 次工程: {guide['next']}",
                f"- 実行状態: `{result.get('status', 'review-only')}`",
                f"- 実行要約: {result.get('summary', 'Review Gateとして実行')}",
                "",
            ]
        )
        if result:
            actual_items = _phase_actual_items(phase, result, state)
            actual_items.extend(_feedback_actual_items(result))
            for label, value in actual_items:
                lines.append(
                    f"- 実際の{label}: {_format_markdown_value(value)}"
                )
        for review in related_reviews:
            lines.append(
                f"- 承認: `{review.get('action')}` "
                f"by `{review.get('decided_by')}`"
                + (
                    f" — {review.get('feedback')}"
                    if review.get("feedback")
                    else ""
                )
            )
        lines.append("")
    lines.extend(
        [
            "## 補足",
            "",
            "このレポートは公開可能な入力、構造化出力、判断理由、承認履歴を"
            "説明します。内部Chain-of-ThoughtやAPIサーバーログは掲載しません。",
            "",
        ]
    )
    return "\n".join(lines)


def _cut_media_paths(state: WorkflowState, cut_id: int) -> tuple[str | None, str | None]:
    """提出Package内での（元画像, 生成動画）の相対パス。

    実体は `_copy_cut_sources` が同じ命名で配置する。
    """
    shots = {
        int(s.get("id", s.get("cut_id", 0))): s
        for s in state.get("phase_results", {})
        .get("director", {})
        .get("data", {})
        .get("shots", [])
    }
    asset = (shots.get(cut_id, {}) or {}).get("asset", {}) or {}
    source = Path(str(asset.get("local_path") or ""))
    source_rel = (
        f"artifacts/sources/cut_{cut_id:02d}_source{source.suffix}"
        if source.name
        else None
    )
    clip = Path(
        str(
            (state.get("production_artifacts", {}).get(str(cut_id)) or {}).get(
                "path"
            )
            or ""
        )
    )
    clip_rel = (
        f"artifacts/cuts/cut_{cut_id:02d}{clip.suffix}" if clip.name else None
    )
    return source_rel, clip_rel


# ホームディレクトリの一般形（macOS: /Users/名前, Linux: /home/名前）。
# レポートを生成した機械と、パスが記録された機械が違う場合（過去runの再生成、
# CI、別環境での検証）でもユーザー名が残らないよう、実行環境の HOME だけに
# 頼らず正規表現でも畳む。
_HOME_PATTERN = re.compile(r"/(?:Users|home)/[^/\"',\s]+/")


def _llm_usage_totals(state: WorkflowState) -> dict[str, Any]:
    """LLM呼び出しのトークン数と費用を、この run の分だけ合計する。

    `runtime/llm_cost_ledger.json` は全run累積なので使えない。各フェーズの
    `phase_results[phase]["llm"]["usage"]` を足し、config の単価で費用を出す。
    古い run では usage が "***" にマスクされていることがあるため、数値化
    できたものだけを集計し、その旨を `available` で返す。
    """
    llm_config = state.get("config", {}).get("llm", {})
    guard = llm_config.get("cost_guard", {})
    in_rate = float(guard.get("input_cost_per_million_usd", 0.0))
    out_rate = float(guard.get("output_cost_per_million_usd", 0.0))

    prompt = completion = 0
    calls = 0
    masked = False
    for result in state.get("phase_results", {}).values():
        usage = ((result or {}).get("llm") or {}).get("usage") or {}
        if not usage:
            continue
        calls += 1
        for key, add in (("prompt_tokens", "p"), ("completion_tokens", "c")):
            raw = usage.get(key)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                masked = True
                continue
            if add == "p":
                prompt += value
            else:
                completion += value

    cost = (prompt * in_rate + completion * out_rate) / 1_000_000
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_usd": round(cost, 6),
        "available": bool(prompt or completion),
        "masked": masked,
        "model": llm_config.get("model") or guard.get("pricing_model", ""),
    }


def _video_cost_summary(state: WorkflowState) -> dict[str, Any]:
    """この run の動画生成の実課金を台帳から読む（推定ではなく実額）。"""
    path = deterministic._work_path(state, "video_cost_ledger.json")
    try:
        ledger = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"spent_usd": 0.0, "generations": []}
    return {
        "spent_usd": float(ledger.get("spent_usd", 0.0)),
        "generations": list(ledger.get("generations", [])),
    }


def _human_intervention_summary(state: WorkflowState) -> dict[str, int]:
    """人間が何回・どう介入したかを数える（自律性の説明に使う）。"""
    counts = {"approve": 0, "retry_with_feedback": 0, "override": 0}
    for review in state.get("reviews", []):
        if str(review.get("decided_by")) != "human":
            continue
        action = str(review.get("action"))
        if action in counts:
            counts[action] += 1
    for cut in state.get("cut_results", {}).values():
        if ((cut or {}).get("qa") or {}).get("decided_by") == "human":
            counts["override"] += 1
    return counts


def _portable_path(value: Any) -> str:
    """提出物に載せるパスから、実行環境固有の部分を取り除く。

    `/Users/<name>/.../Agewec/runtime/...` のような絶対パスをそのまま
    載せると、提出先にユーザー名とディレクトリ構成が見える。まずリポジトリからの
    相対表記へ畳み、残った絶対パスはホーム部分を `~/` に置き換える。
    """
    text = str(value)
    if not text:
        return text
    for root in (
        runtime_paths().project_root,
        Path.home(),
    ):
        text = text.replace(str(root) + "/", "").replace(str(root), ".")
    return _HOME_PATTERN.sub("~/", text)


# バックエンドごとの説明文。_PHASE_PRESENTATION は静的な辞書なので、
# ComfyUI/LTX固定の文言を実際に使った経路へ差し替える。
# （Runwayで実行したのに「ComfyUIが実行できる技術パラメータへ変換」と
#   書かれていると、レポートが事実と食い違う）
_BACKEND_PRESENTATION: dict[str, dict[str, dict[str, Any]]] = {
    "runway": {
        "support_video_creator": {
            "purpose": (
                "演出指示を、動画生成APIが受け付ける技術パラメータへ"
                "安全に変換する。"
            ),
            "inputs": [
                "画像パスと生成Prompt",
                "Storyboard秒数",
                "モデルの許容尺・解像度・単価",
            ],
            "process": (
                "秒数をモデルの許容尺へ丸め、解像度・seed・概算費用を確定する。"
            ),
        },
        "image_video_production": {
            "inputs": [
                "入力画像",
                "positive/negative prompt",
                "model / duration / ratio / seed",
            ],
            "process": (
                "Runway APIへ投入し、完了を待って生成動画と実行情報・"
                "実課金額を保存する。"
            ),
        },
    },
}


def _guide_for_backend(
    guide: dict[str, Any],
    state: WorkflowState,
) -> dict[str, Any]:
    backend = str(
        state.get("config", {}).get("production", {}).get("backend", "")
    )
    override = _BACKEND_PRESENTATION.get(backend, {}).get(guide["id"])
    return {**guide, **override} if override else guide


def _request_summary(item: dict[str, Any]) -> str:
    """ProductionRequest 1件を、その契約に存在する項目だけで1行にする。

    Runway契約には frames / steps / fps が無く（尺と解像度はモデル側が決める）、
    Comfy/LTX契約にはモデル名や許容尺が無い。両方を同じ書式で出そうとすると
    「None frames / Nonefps / None steps」のように存在しない項目が None として
    表示されるため、契約ごとに項目を選ぶ。
    """
    parts: list[str] = []
    if str(item.get("generation_mode") or "") == "text_to_video":
        parts.append("t2v")
    width, height = item.get("width"), item.get("height")
    if width and height:
        parts.append(f"{width}x{height}")
    elif item.get("resolution"):
        parts.append(str(item["resolution"]))

    if str(item.get("request_contract", "")) == "runway_model_native":
        if item.get("model"):
            parts.append(str(item["model"]))
        seconds = item.get("effective_seconds") or item.get("actual_seconds")
        if seconds:
            parts.append(f"{float(seconds):g}秒")
    else:
        for value, unit in (
            (item.get("frames"), " frames"),
            (item.get("fps"), "fps"),
            (item.get("steps"), " steps"),
        ):
            if value is not None:
                parts.append(f"{value}{unit}")

    if item.get("seed") is not None:
        parts.append(f"seed {item['seed']}")
    return ", ".join(parts) if parts else "—"


def _generation_conditions(
    request: dict[str, Any],
    qa: dict[str, Any],
) -> str:
    """解像度・fps・尺を「実際に出力された値」優先で1行にまとめる。

    fps はバックエンドによって Request に存在しない（Runwayはモデル側が決める
    ためリクエスト項目が無く、LTXのときだけ入る）。Requestだけを見ると空文字に
    なり「1280×720 / fps / ...」と壊れた表示になるため、QAがffprobeで実測した
    `technical` を第一の情報源とし、無い場合のみ Request で補う。
    項目が両方から得られないときは、その項目ごと省く。
    """
    technical = qa.get("technical") or {}
    parts: list[str] = []

    width = technical.get("width") or request.get("width")
    height = technical.get("height") or request.get("height")
    if width and height:
        parts.append(f"{width}×{height}")

    fps = technical.get("fps") or request.get("fps")
    if fps:
        value = float(fps)
        parts.append(f"{value:g}fps")

    seconds = (
        technical.get("duration_seconds")
        or request.get("actual_seconds")
        or request.get("requested_seconds")
    )
    if seconds:
        parts.append(f"{float(seconds):.2f}秒")

    return " / ".join(parts) if parts else "—"


# カード表示で同じ内容を示す項目。文字列の羅列を二重に出さないため、
# カードがあるフェーズではこれらの行を省く（絶対パスの露出もここで消える）。
_CARD_COVERED_ITEMS: dict[str, set[str]] = {
    "director": {"カット別演出"},
    "image_video_production": {"生成済みカット", "生成動画"},
}


def _phase_visual_cards(phase: str, state: WorkflowState) -> str:
    """演出設計・映像生成の「実物」を並べたカードを返す。

    テキストの羅列では「この写真にこの指示でこう動いた」が判断できないため、
    元画像・プロンプト・カメラワーク・生成映像を1カットずつ並べる。
    """
    if phase not in {"director", "image_video_production"}:
        return ""
    results = state.get("phase_results", {})
    shots = sorted(
        results.get("director", {}).get("data", {}).get("shots", []),
        key=lambda s: int(s.get("id", s.get("cut_id", 0))),
    )
    if not shots:
        return ""
    cuts = {
        int(c.get("id", 0)): c
        for c in results.get("writer_storyboard", {}).get("data", {}).get("cuts", [])
    }
    requests = state.get("production_requests", {})
    qa_results = state.get("cut_qa_results", {})
    attempts = state.get("cut_attempts", {})

    box = (
        "background:#fafbfc;border:1px solid #e2e6ec;border-radius:10px;"
        "padding:12px 14px;margin-bottom:10px;"
    )
    label = "font-size:11px;color:#5b6570;margin:8px 0 2px;"
    # レポート全体のCSSは pre{background:#202723;color:#dce8e2}（暗い背景に
    # 明るい文字）。カードでは背景を明るくするため、文字色も必ず上書きする。
    # color を省くと「ほぼ白地に明るいグレー文字」となりコントラスト比が
    # 1.13:1 まで落ち、本文が読めなくなる（WCAG基準は4.5:1）。
    pre = (
        "white-space:pre-wrap;background:#f0f3f7;color:#1f2933;"
        "border-radius:6px;padding:8px;font-size:12px;margin:0;"
    )
    cards = []
    for shot in shots:
        cut_id = int(shot.get("id", shot.get("cut_id", 0)))
        cut = cuts.get(cut_id, {})
        asset = shot.get("asset", {}) or {}
        source_rel, clip_rel = _cut_media_paths(state, cut_id)
        request = requests.get(str(cut_id), {}) or {}
        qa = qa_results.get(str(cut_id), {}) or {}
        head = (
            f"<div style='display:flex;align-items:baseline;gap:8px;"
            f"flex-wrap:wrap;'><strong style='font-size:14px;'>Cut {cut_id}</strong>"
            f"<span style='font-size:13px;'>{html.escape(str(cut.get('name', '')))}</span>"
            f"<span style='font-size:11px;color:#8b95a1;'>"
            f"{html.escape(str(cut.get('seconds', '')))}秒 / "
            f"{html.escape(str(cut.get('time_of_day', '')))}</span></div>"
        )

        # text_to_video のカットには元になった写真が存在しない。
        # 「素材が抜けている」ではなく「文章から作った」と読めるようにする。
        is_text_to_video = (
            str(shot.get("generation_mode") or "image_to_video")
            == "text_to_video"
        )
        no_source_note = (
            "<span style='color:#8b95a1;font-size:12px;'>"
            + ("完全生成映像（元画像なし）" if is_text_to_video else "元画像なし")
            + "</span>"
        )

        if phase == "director":
            # 選んだ写真と、その写真に与える指示を並べる
            media = (
                f"<img src='{html.escape(source_rel)}' "
                "style='width:100%;border-radius:8px;display:block;'>"
                if source_rel
                else no_source_note
            )
            asset_line = (
                "<div style='font-size:12px;'>"
                "文章のみから生成（Text to Video）</div>"
                if is_text_to_video
                else (
                    "<div style='font-size:12px;'>"
                    f"{html.escape(str(asset.get('title', '')))}"
                    f" <code>{html.escape(str(asset.get('asset_id', '')))}</code>"
                    "</div>"
                )
            )
            model_line = html.escape(str(shot.get("model") or "既定モデル"))
            cards.append(
                f"<div style='{box}'>{head}"
                "<div style='display:grid;grid-template-columns:220px minmax(0,1fr);"
                "gap:14px;margin-top:10px;'>"
                f"<div>{media}"
                f"<p style='{label}'>"
                + ("生成方式" if is_text_to_video else "使用素材")
                + "</p>"
                f"{asset_line}"
                f"<p style='{label}'>使用モデル</p>"
                f"<div style='font-size:12px;'>{model_line}</div>"
                "</div><div>"
                f"<p style='{label}'>カメラワーク</p>"
                f"<div style='font-size:13px;'>"
                f"{html.escape(str(shot.get('camera_motion', '—')))}</div>"
                f"<p style='{label}'>生成プロンプト</p>"
                f"<pre style='{pre}'>"
                f"{html.escape(str(shot.get('positive_prompt', '')))}</pre>"
                f"<p style='{label}'>避ける表現</p>"
                f"<pre style='{pre}'>"
                f"{html.escape(str(shot.get('negative_prompt', '') or '—'))}</pre>"
                f"<p style='{label}'>この演出を選んだ理由</p>"
                f"<div style='font-size:12px;'>"
                f"{html.escape(str(shot.get('rationale', '') or '—'))}</div>"
                "</div></div></div>"
            )
        else:
            # 元画像 → 生成映像 を並べ、生成条件とQA結果を添える
            left = (
                f"<img src='{html.escape(source_rel)}' "
                "style='width:100%;border-radius:8px;display:block;'>"
                if source_rel
                else no_source_note
            )
            right = (
                f"<video src='{html.escape(clip_rel)}' controls "
                "style='width:100%;border-radius:8px;display:block;background:#000;'>"
                "</video>"
                if clip_rel
                else "<span style='color:#8b95a1;font-size:12px;'>未生成</span>"
            )
            verdict = str(qa.get("verdict", "—"))
            issues = qa.get("issues", [])
            issue_text = (
                "<br>".join(
                    html.escape(f"{i.get('code')}: {i.get('description')}")
                    for i in issues
                )
                or "検出された問題はありません"
            )
            cards.append(
                f"<div style='{box}'>{head}"
                "<div style='display:grid;grid-template-columns:1fr 1fr;"
                "gap:12px;margin-top:10px;'>"
                f"<div><p style='{label}'>"
                + ("入力（なし）" if is_text_to_video else "元画像")
                + f"</p>{left}</div>"
                f"<div><p style='{label}'>生成された映像</p>{right}</div>"
                "</div>"
                f"<p style='{label}'>生成条件</p>"
                "<div style='font-size:12px;color:#5b6570;'>"
                f"{html.escape(_generation_conditions(request, qa))} / "
                f"seed {html.escape(str(request.get('seed', '—')))} / "
                f"試行 {html.escape(str(attempts.get(str(cut_id), 1)))}回目</div>"
                f"<p style='{label}'>QA結果: {html.escape(verdict)}</p>"
                f"<div style='font-size:12px;color:#5b6570;'>{issue_text}</div>"
                "</div>"
            )
    title = "選定した写真と演出指示" if phase == "director" else "生成された映像"
    # .actual-row は grid-template-columns:180px 1fr。カードを直下に並べると
    # 「見出し・カード1」で1行目が埋まり、カード2以降が180pxの狭い列へ
    # 送られて潰れる。カード全体を1つの器に入れ、行の子要素を常に2つに保つ。
    return (
        f"<div class='actual-row'><h4>{title}</h4>"
        f"<div class='card-stack'>{''.join(cards)}</div>"
        "</div>"
    )


def _run_summary_html(state: WorkflowState) -> str:
    """実行サマリー（時間・費用・人間の介入）をレポート末尾に置く。

    審査側が最初に知りたいのは「いくらで、どれだけの時間で、人がどれだけ
    手を入れて作ったか」であり、フェーズ本文を全部読まないと分からない状態を
    避ける。数値はすべてこの run の実績（見積ではない）。
    """
    timing_summary = timing.summarize(state)
    llm = _llm_usage_totals(state)
    video = _video_cost_summary(state)
    human = _human_intervention_summary(state)
    total_cost = llm["cost_usd"] + video["spent_usd"]
    titles = {g["id"]: g["title"] for g in _PHASE_PRESENTATION}
    numbers = {g["id"]: g["number"] for g in _PHASE_PRESENTATION}
    # 図に出さない内部ノードも時間は計測される。phase名のまま出すと
    # 読み手が「これは何の工程か」を判断できないので、日本語名を与える。
    titles.setdefault("commit_cut_qa", "カット判定の確定（内部処理）")

    def minutes(seconds: float) -> str:
        seconds = float(seconds or 0)
        return (
            f"{seconds:.1f}秒"
            if seconds < 60
            else f"{int(seconds // 60)}分{seconds % 60:.0f}秒"
        )

    kpis = [
        ("総所要時間（処理のみ）", minutes(timing_summary["total_phase_seconds"])),
        ("総費用", f"${total_cost:.2f}"),
        (
            "人間の介入",
            f"承認{human['approve']} / 差し戻し{human['retry_with_feedback']}"
            + (f" / 上書き{human['override']}" if human["override"] else ""),
        ),
        ("最も時間を要した工程", str(timing_summary.get("slowest_phase") or "—")),
    ]
    kpi_html = "".join(
        "<div class='kpi'>"
        f"<span class='kpi-label'>{html.escape(label)}</span>"
        f"<strong class='kpi-value'>{html.escape(value)}</strong></div>"
        for label, value in kpis
    )

    # timing.summarize は所要時間の降順で返すが、レポートでは工程の実行順に
    # 並べる（01→02→…）。番号を持たない内部ノードは末尾へ回す。
    order = {guide["id"]: index for index, guide in enumerate(_PHASE_PRESENTATION)}
    phases_in_flow_order = sorted(
        timing_summary.get("phases", []),
        key=lambda row: order.get(row.get("phase"), len(order)),
    )
    phase_rows = "".join(
        "<tr>"
        f"<td>{html.escape(numbers.get(row['phase'], '—'))}</td>"
        f"<td>{html.escape(titles.get(row['phase'], row['phase']))}</td>"
        f"<td class='num'>{html.escape(str(row.get('runs', 1)))}</td>"
        f"<td class='num'>{html.escape(minutes(row.get('cumulative_duration_seconds', 0)))}</td>"
        f"<td>{html.escape(str(row.get('last_status', '')))}</td>"
        "</tr>"
        for row in phases_in_flow_order
    ) or "<tr><td colspan='5'>計測データがありません</td></tr>"

    def video_row(gen: dict[str, Any]) -> str:
        seconds = float(gen.get("billed_seconds", 0) or 0)
        model = str(gen.get("model") or gen.get("provider") or "—")
        job = str(gen.get("job_id") or "—")[:8]
        return (
            "<tr>"
            f"<td class='num'>Cut {html.escape(str(gen.get('cut_id')))}</td>"
            f"<td>{html.escape(model)}</td>"
            f"<td class='num'>{seconds:g}秒</td>"
            f"<td class='num'>${float(gen.get('cost_usd', 0)):.2f}</td>"
            f"<td class='mono'>{html.escape(job)}</td>"
            "</tr>"
        )

    video_rows = "".join(
        video_row(gen) for gen in video["generations"]
    ) or "<tr><td colspan='5'>課金を伴う生成はありません</td></tr>"

    token_note = (
        f"AI（LLM）: {llm['calls']}回の呼び出し / "
        f"{llm['total_tokens']:,}トークン / ${llm['cost_usd']:.4f}"
        f"（{html.escape(str(llm['model']))}）"
        if llm["available"]
        else "AI（LLM）: このrunではトークン使用量が記録されていません"
    )
    video_note = f"動画生成: ${video['spent_usd']:.2f}（実課金）"

    return (
        "<section class='phase-card run-summary-card'>"
        "<h2>実行サマリー</h2>"
        f"<div class='kpi-grid'>{kpi_html}</div>"
        "<h3>工程別の所要時間</h3>"
        "<table class='summary-table'><thead><tr>"
        "<th>#</th><th>工程</th><th>実行回数</th><th>所要時間</th><th>状態</th>"
        f"</tr></thead><tbody>{phase_rows}</tbody></table>"
        "<h3>動画生成の実課金</h3>"
        "<table class='summary-table'><thead><tr>"
        "<th>カット</th><th>モデル</th><th>課金尺</th><th>費用</th><th>Job</th>"
        f"</tr></thead><tbody>{video_rows}</tbody></table>"
        "<h3>費用の内訳</h3>"
        f"<p class='cost-line'>{token_note}</p>"
        f"<p class='cost-line'>{video_note}</p>"
        f"<p class='cost-total'>合計 ${total_cost:.2f}</p>"
        "<p class='muted'>総所要時間は各工程の処理時間の合計であり、"
        "承認画面での待ち時間は含みません。</p>"
        "</section>"
    )


def _process_html(state: WorkflowState, video_name: str) -> str:
    phase_results = state.get("phase_results", {})
    reviews = state.get("reviews", [])
    flow_nodes = []
    cards = []
    for static_guide in _PHASE_PRESENTATION:
        # 実際に使ったバックエンドに合わせて説明文を差し替える
        guide = _guide_for_backend(static_guide, state)
        phase = guide["id"]
        result = phase_results.get(phase, {})
        status = str(result.get("status", "review-only"))
        flow_nodes.append(
            "<div class='flow-node'>"
            f"<span>{html.escape(guide['number'])}</span>"
            f"<strong>{html.escape(guide['title'])}</strong>"
            f"<small>{html.escape(guide['kind'])}</small>"
            "</div>"
        )
        # 演出設計と映像生成は、文字列の羅列では判断できないため
        # 実物（元画像・生成映像）を並べたカードを先頭に置く。
        cards_html = _phase_visual_cards(phase, state) if result else ""
        covered = _CARD_COVERED_ITEMS.get(phase, set()) if cards_html else set()
        actual = cards_html + "".join(
            "<div class='actual-row'>"
            f"<h4>{html.escape(label)}</h4>"
            f"{_render_html_value(value)}"
            "</div>"
            for label, value in (
                (
                    _phase_actual_items(phase, result, state)
                    + _feedback_actual_items(result)
                )
                if result
                else []
            )
            # カードが同じ内容を示す項目は重複するので出さない
            if label not in covered
        )
        related_reviews = [
            review
            for review in reviews
            if review.get("phase") == phase
            or review.get("source_phase") == phase
        ]
        review_html = "".join(
            "<div class='review-row'>"
            f"<strong>{html.escape(str(review.get('action')))}</strong>"
            f"<span>{html.escape(str(review.get('decided_by')))}</span>"
            + (
                f"<p>{html.escape(str(review.get('feedback')))}</p>"
                if review.get("feedback")
                else ""
            )
            + "</div>"
            for review in related_reviews
        ) or "<p class='muted'>この工程の承認記録はありません。</p>"
        technical = {
            "status": result.get("status"),
            "attempt": result.get("attempt"),
            "confidence": result.get("confidence"),
            "warnings": result.get("warnings", []),
            "blocking_issues": result.get("blocking_issues", []),
            "artifacts": result.get("artifacts", []),
        }
        payload = html.escape(
            _portable_path(
                json.dumps(
                    deterministic._sanitized(technical),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        )
        actual_html = (
            actual
            if actual
            else "<p class='muted'>構造化成果物はありません。</p>"
        )
        cards.append(
            "<article class='phase-card'>"
            "<header>"
            f"<span class='phase-number'>{html.escape(guide['number'])}</span>"
            "<div>"
            f"<h2>{html.escape(guide['title'])}</h2>"
            f"<span class='tag'>{html.escape(guide['kind'])}</span>"
            f"<span class='status status-{html.escape(status)}'>"
            f"{html.escape(status)}</span>"
            "</div></header>"
            "<section class='purpose'>"
            "<h3>何のためのノードか</h3>"
            f"<p>{html.escape(guide['purpose'])}</p></section>"
            "<section class='contract-grid'>"
            "<div><h3>入力</h3>"
            f"<p class='source'>入力元: {html.escape(guide['input_source'])}</p>"
            "<ul>"
            + "".join(
                f"<li>{html.escape(item)}</li>"
                for item in guide["inputs"]
            )
            + "</ul></div>"
            "<div><h3>処理</h3>"
            f"<p>{html.escape(guide['process'])}</p></div>"
            "<div><h3>出力</h3>"
            f"<p><code>{html.escape(guide['output'])}</code></p>"
            f"<p class='source'>次: {html.escape(guide['next'])}</p></div>"
            "</section>"
            "<section class='actual'>"
            "<h3>この実行で生成・判断された内容</h3>"
            f"<p class='run-summary'>{html.escape(str(result.get('summary', 'Review Gateとして実行')))}</p>"
            f"{actual_html}"
            "</section>"
            "<section class='reviews'><h3>承認・修正履歴</h3>"
            f"{review_html}</section>"
            "<details class='technical'><summary>技術情報</summary>"
            f"<pre>{payload}</pre></details>"
            "</article>"
        )
    arrows = "<span class='flow-arrow'>→</span>".join(flow_nodes)
    summary_section = _run_summary_html(state)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGEWEC Process Report</title>
<style>
*{{box-sizing:border-box}}body{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;
max-width:1180px;margin:0 auto;padding:34px 22px 80px;background:#f4f3ef;
color:#1c2522;line-height:1.65}}h1,h2,h3,h4,p{{margin-top:0}}
.hero,.workflow,.phase-card{{background:#fff;border:1px solid #d9ddd8;
border-radius:18px;box-shadow:0 8px 28px rgba(20,42,34,.06)}}
.hero{{padding:28px;margin-bottom:24px}}.hero-grid{{display:grid;
grid-template-columns:1.5fr 1fr;gap:26px;align-items:center}}
.eyebrow{{color:#0b7257;font-weight:800;letter-spacing:.08em;
text-transform:uppercase}}video{{width:100%;max-height:460px;background:#000;
border-radius:12px}}.workflow{{padding:24px;margin-bottom:28px}}
.flow{{display:flex;align-items:center;gap:9px;overflow-x:auto;padding:10px 0 16px}}
.flow-node{{min-width:142px;padding:12px;border:1px solid #cdd8d3;
border-radius:12px;background:#f7fbf9;display:grid;gap:3px}}
.flow-node span,.phase-number{{font-weight:900;color:#0b7257}}
.flow-node small{{color:#68736e}}.flow-arrow{{font-size:22px;color:#799087}}
.loop-note{{background:#edf7f3;border-left:4px solid #0b7257;
padding:12px 15px;border-radius:8px;margin:0}}
.phase-card{{padding:24px;margin:18px 0}}.phase-card>header{{display:flex;
gap:16px;align-items:flex-start;border-bottom:1px solid #e5e8e5;padding-bottom:16px}}
.phase-number{{font-size:26px;min-width:56px}}.phase-card h2{{margin-bottom:5px}}
.tag,.status{{display:inline-block;padding:3px 9px;border-radius:999px;
font-size:12px;font-weight:750;margin-right:6px}}.tag{{background:#e9f3ef;color:#155d49}}
.status{{background:#eceeec;color:#58615e}}.status-success{{background:#dff5e9;
color:#11613e}}.status-error{{background:#fde6e3;color:#a1332b}}
.purpose{{padding:18px 0 2px}}.contract-grid{{display:grid;
grid-template-columns:1fr 1fr 1fr;gap:14px;margin:14px 0 22px}}
.contract-grid>div{{background:#f7f7f4;border-radius:12px;padding:16px}}
.contract-grid h3,.actual h3,.reviews h3,.purpose h3{{font-size:15px;
color:#52615b;margin-bottom:7px}}.source,.muted{{color:#74807b}}
.actual{{border-top:1px solid #e5e8e5;padding-top:20px}}.run-summary{{
font-size:18px;font-weight:700}}.actual-row{{display:grid;
grid-template-columns:180px 1fr;gap:16px;padding:10px 0;border-bottom:1px dashed #dde2de}}
.card-stack{{min-width:0}}
.run-summary-card{{padding:26px 28px;margin-top:26px}}
.run-summary-card h2{{font-size:20px;margin-bottom:16px}}
.run-summary-card h3{{font-size:14px;color:#51625b;margin:22px 0 8px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px}}
.kpi{{background:#f4f6f4;border:1px solid #e2e6e2;border-radius:10px;
padding:12px 14px}}
.kpi-label{{display:block;font-size:11px;color:#5b6570;margin-bottom:4px}}
.kpi-value{{font-size:17px;color:#1c2522}}
.summary-table{{width:100%;border-collapse:collapse;font-size:13px}}
.summary-table th{{text-align:left;color:#5b6570;font-weight:600;
font-size:11px;border-bottom:1px solid #dde2de;padding:6px 8px}}
.summary-table td{{border-bottom:1px solid #eef1ee;padding:6px 8px}}
.summary-table td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.summary-table td.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
color:#5b6570}}
.cost-line{{font-size:13px;margin:4px 0;color:#3a463f}}
.cost-total{{font-size:16px;font-weight:700;margin:10px 0 0}}
.actual-row h4{{font-size:14px;color:#51625b;margin:0}}ul{{margin:6px 0;
padding-left:21px}}dl{{display:grid;grid-template-columns:minmax(120px,.35fr) 1fr;
gap:6px 14px;margin:0}}dt{{font-weight:700}}dd{{margin:0}}code{{background:#ecefeb;
padding:3px 6px;border-radius:5px}}.reviews{{padding-top:20px}}
.review-row{{display:grid;grid-template-columns:140px 100px 1fr;gap:10px;
padding:10px 12px;background:#f7fbf9;border-radius:9px;margin:7px 0}}
.review-row p{{margin:0}}.technical{{margin-top:18px}}summary{{font-weight:700;
cursor:pointer;color:#61706a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;
background:#202723;color:#dce8e2;padding:14px;border-radius:9px;font-size:12px}}
.footer{{margin-top:30px;color:#68736e}}
@media(max-width:760px){{.hero-grid,.contract-grid{{grid-template-columns:1fr}}
.actual-row{{grid-template-columns:1fr}}.review-row{{grid-template-columns:1fr}}
body{{padding:18px 12px 50px}}}}
</style></head><body>
<section class="hero"><div class="hero-grid"><div>
<p class="eyebrow">Traceable AI Production</p>
<h1>AGEWEC 制作プロセス</h1>
<p>AIが何を受け取り、なぜ判断し、何を次工程へ渡したかを、
最終動画と一緒に追跡できるレポートです。</p>
<p>Run ID: <code>{html.escape(str(state.get('run_id')))}</code><br>
目標尺: {html.escape(str(state.get('project', {}).get('target_duration_seconds')))}秒</p>
</div><video controls src="{html.escape(video_name)}"></video></div></section>
<section class="workflow"><h2>全体ワークフロー</h2>
<p>左から右へ成果物が受け渡されます。各ノード後のReview Gateは、
設定に応じて人間またはポリシーが承認します。</p>
<div class="flow">{arrows}</div>
<p class="loop-note"><strong>修正ループ:</strong> Cut QAは問題に応じて
Image / Video Production、Support Video Creator、Director、Asset Curatorへ戻り、
Review Boardの修正はPost Productionへ戻ります。</p></section>
<main>{''.join(cards)}</main>
{summary_section}
<p class="footer">公開可能な入力、構造化出力、判断理由、承認履歴を掲載しています。
内部Chain-of-Thought、APIキー、AIサーバーの生ログは掲載しません。
完全な機械可読証跡はprovenance.jsonに保存されています。</p>
</body></html>"""
