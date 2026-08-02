"""カットごとのレビュー画面（読み取り専用HTML）の生成。

【本番経路: 現役】cut_visual_qa の完了時に呼ばれ、`work/review.html` を更新する。

目的は「人間が実物を見て判断できるようにする」こと。ボタンは持たず、
判断・修正指示はターミナルの Review Gate から入力する（双方向通信を避け、
締切前でも安全に運用できる構成）。

各カットについて次を並べる:
    元画像（選定理由・スコア） / プロンプト・カメラ指示 / 生成動画 / QAフレーム / QA結果
"""
from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

_STATUS_STYLE = {
    "pass": ("#0f6e56", "#e3f5ee"),
    "revise": ("#a34", "#fdecec"),
    "pending": ("#8a5a00", "#fdf3e0"),
}


def _rel(target: str | None, base: Path) -> str | None:
    """HTMLからの相対パスを返す（file:// で開けるように）。"""
    if not target:
        return None
    try:
        return os.path.relpath(str(target), str(base))
    except ValueError:
        return str(target)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _cut_section(
    cut: dict[str, Any],
    shot: dict[str, Any],
    request: dict[str, Any],
    artifact: dict[str, Any] | None,
    qa: dict[str, Any] | None,
    frames: list[str],
    base: Path,
) -> str:
    cut_id = cut.get("id")
    asset = shot.get("asset", {}) or {}
    verdict = (qa or {}).get("verdict", "pending")
    color, bg = _STATUS_STYLE.get(verdict, _STATUS_STYLE["pending"])

    source_rel = _rel(asset.get("local_path") or request.get("image_path"), base)
    video_rel = _rel((artifact or {}).get("path"), base)

    issues = (qa or {}).get("issues", [])
    issue_html = "".join(
        f"<li><b>{_esc(i.get('code'))}</b> "
        f"({_esc(i.get('severity'))}) {_esc(i.get('description'))}</li>"
        for i in issues
    ) or "<li class='muted'>技術的な問題は検出されていません</li>"

    frame_html = "".join(
        f"<img src='{_esc(_rel(f, base))}' alt='QAフレーム'>" for f in frames
    ) or "<span class='muted'>フレーム未抽出</span>"

    return f"""
<section class="cut">
  <div class="cut-head">
    <h2>Cut {_esc(cut_id)} — {_esc(cut.get('name'))}</h2>
    <span class="badge" style="color:{color};background:{bg}">{_esc(verdict)}</span>
  </div>
  <p class="scene">{_esc(cut.get('scene'))}</p>
  <div class="meta">
    <span>{_esc(cut.get('time_of_day'))}</span>
    <span>{_esc(cut.get('location'))}</span>
    <span>{_esc(cut.get('visual_role'))}</span>
    <span>{_esc(cut.get('seconds'))}秒</span>
    <span>seed {_esc(request.get('seed'))}</span>
  </div>

  <div class="grid">
    <div class="panel">
      <h3>元画像</h3>
      {f"<img class='source' src='{_esc(source_rel)}' alt='元画像'>"
       if source_rel else "<span class='muted'>画像なし</span>"}
      <p class="cap">{_esc(asset.get('title'))} <code>{_esc(asset.get('asset_id'))}</code></p>
      <p class="reason">{_esc(asset.get('selection_reason'))}</p>
    </div>
    <div class="panel">
      <h3>生成された動画</h3>
      {f"<video class='clip' controls src='{_esc(video_rel)}'></video>"
       if video_rel else "<span class='muted'>未生成</span>"}
      <p class="cap">QAフレーム</p>
      <div class="frames">{frame_html}</div>
    </div>
  </div>

  <details class="detail">
    <summary>プロンプト・演出指示</summary>
    <p class="label">Positive</p><pre>{_esc(shot.get('positive_prompt'))}</pre>
    <p class="label">Negative</p><pre>{_esc(shot.get('negative_prompt'))}</pre>
    <p class="label">Camera</p><pre>{_esc(shot.get('camera_motion'))}</pre>
    <p class="label">Rationale</p><pre>{_esc(shot.get('rationale'))}</pre>
  </details>

  <details class="detail">
    <summary>QA結果</summary>
    <ul>{issue_html}</ul>
  </details>
</section>
"""


def build_review_page(state: dict[str, Any], output_path: Path) -> Path:
    """現時点までのカット状況をHTMLにして保存し、そのパスを返す。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base = output_path.parent

    results = state.get("phase_results", {})
    cuts = (
        results.get("writer_storyboard", {}).get("data", {}).get("cuts", [])
    )
    shots = {
        int(s.get("id", s.get("cut_id", 0))): s
        for s in results.get("director", {}).get("data", {}).get("shots", [])
    }
    requests = state.get("production_requests", {})
    artifacts = state.get("production_artifacts", {})
    qa_results = state.get("cut_qa_results", {})

    # 再生成すると同じカットのQAフレームが積み上がるため、
    # 各カットで「最後に抽出された3枚」だけを表示する。
    frames_by_cut: dict[int, list[str]] = {}
    for item in state.get("artifacts", []):
        if item.get("kind") == "qa_frame" and item.get("cut_id") is not None:
            frames_by_cut.setdefault(int(item["cut_id"]), []).append(
                str(item.get("path"))
            )
    frames_by_cut = {
        cut_id: list(dict.fromkeys(paths))[-3:]
        for cut_id, paths in frames_by_cut.items()
    }

    sections = []
    for cut in cuts:
        cut_id = int(cut.get("id", 0))
        sections.append(
            _cut_section(
                cut,
                shots.get(cut_id, {}),
                requests.get(str(cut_id), {}),
                artifacts.get(str(cut_id)),
                qa_results.get(str(cut_id)),
                frames_by_cut.get(cut_id, []),
                base,
            )
        )

    current = state.get("current_cut_id")
    approved = state.get("approved_cut_ids", [])
    run_id = state.get("run_id", "")
    body = "".join(sections) or "<p class='muted'>まだカットがありません</p>"

    output_path.write_text(
        f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cut Review — {_esc(run_id)}</title>
<style>
:root{{--navy:#203864;--ink:#1f2733;--sub:#5b6570;--line:#e2e6ec;--bg:#f6f8fb;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);line-height:1.6;
 font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",sans-serif}}
.wrap{{max-width:1000px;margin:0 auto;padding:28px 22px 60px}}
h1{{font-size:20px;color:var(--navy);margin:0 0 4px}}
.status{{font-size:13px;color:var(--sub);margin:0 0 22px}}
.cut{{background:#fff;border:1px solid var(--line);border-radius:12px;
 padding:16px 18px;margin-bottom:18px}}
.cut-head{{display:flex;align-items:center;gap:10px}}
h2{{font-size:16px;color:var(--navy);margin:0}}
h3{{font-size:12px;color:var(--sub);margin:0 0 6px;font-weight:600}}
.badge{{font-size:11px;padding:2px 10px;border-radius:20px;font-weight:600}}
.scene{{font-size:13px;color:var(--sub);margin:4px 0 8px}}
.meta{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
.meta span{{font-size:11px;background:#f0f3f7;border-radius:4px;padding:2px 8px;color:var(--sub)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:720px){{.grid{{grid-template-columns:1fr}}}}
.panel{{background:#fafbfc;border-radius:10px;padding:12px}}
.source,.clip{{width:100%;border-radius:8px;background:#000;display:block}}
.cap{{font-size:12px;color:var(--sub);margin:8px 0 2px}}
.reason{{font-size:12px;margin:2px 0 0}}
.frames{{display:flex;gap:6px}}
.frames img{{width:33%;border-radius:6px}}
code{{background:#eef1f5;border-radius:4px;padding:1px 5px;font-size:11px}}
.detail{{margin-top:10px;font-size:13px}}
summary{{cursor:pointer;color:var(--navy);font-weight:600;font-size:13px}}
.label{{font-size:11px;color:var(--sub);margin:8px 0 2px}}
pre{{white-space:pre-wrap;background:#f3f5f8;border-radius:6px;padding:8px;
 font-size:12px;margin:0}}
.muted{{color:#98a2ae;font-size:12px}}
ul{{margin:6px 0;padding-left:18px;font-size:12px}}
</style></head><body><div class="wrap">
<h1>カットレビュー</h1>
<p class="status">run: {_esc(run_id)} ／ 確認中: Cut {_esc(current)} ／
 承認済み: {_esc(', '.join(map(str, approved)) or 'なし')}
 <br>判断はターミナルの Review Gate から入力してください（この画面は読み取り専用）。</p>
{body}
</div></body></html>""",
        encoding="utf-8",
    )
    return output_path
