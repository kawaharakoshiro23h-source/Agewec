"""[LEGACY v1] 8つのノード（モック実装）。

実装段階では各ノードの中身を実モデル/実ツール呼び出しに差し替える。
グラフの配線・状態の受け渡し・チェックポイントの位置はこのままで動く。
"""
from __future__ import annotations

import json
import time
from typing import Any

from langgraph.types import interrupt

from . import assets, llm
from .backends import get_backends
from .state import AgentState, Cut


def _log(state: AgentState, node: str, msg: str) -> dict[str, Any]:
    """証跡ログに1行足すための更新差分を返す。"""
    entry = {"t": round(time.time(), 3), "node": node, "msg": msg}
    return {"log": state.get("log", []) + [entry]}


# --- 1. Planner -----------------------------------------------------------
_PLANNER_SYSTEM = (
    "あなたは観光プロモーション動画の絵コンテ作家です。"
    "指定された都市と賞のテーマに沿って、4カットの構成をJSONで返します。"
    "余計な説明やコードフェンスは付けず、JSON配列だけを出力してください。"
)


def _mock_cuts(award: str, theme: str) -> list[Cut]:
    return [
        Cut(id=1, scene_desc="オープニング", image_prompt=f"{theme} opening, cinematic",
            motion_prompt="slow push in", narration="ようこそ、北九州へ。"),
        Cut(id=2, scene_desc="見せ場1", image_prompt=f"{award} 見せ場1",
            motion_prompt="pan right", narration="工場夜景が水面に揺れる。"),
        Cut(id=3, scene_desc="見せ場2", image_prompt=f"{award} 見せ場2",
            motion_prompt="tilt up", narration="歴史と未来が交差する街。"),
        Cut(id=4, scene_desc="締め", image_prompt=f"{theme} ending",
            motion_prompt="slow zoom out", narration="さあ、会いに行こう。"),
    ]


def _parse_cuts(text: str) -> list[Cut]:
    """LLM出力(JSON)をCutのリストに変換。コードフェンスが付いても拾う。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1].lstrip("json").strip()
    data = json.loads(t)
    cuts = []
    for i, d in enumerate(data, start=1):
        cuts.append(Cut(
            id=i,
            scene_desc=d.get("scene_desc", ""),
            image_prompt=d.get("image_prompt", ""),
            motion_prompt=d.get("motion_prompt", ""),
            narration=d.get("narration", ""),
        ))
    return cuts


def planner(state: AgentState) -> dict[str, Any]:
    award = state.get("target_award", "観光賞")
    theme = state.get("theme", "北九州の魅力")

    # ローカルLLMが使えれば実生成、無理ならモックにフォールバック
    prompt = (
        f"都市: 北九州 / 賞テーマ: {award} / 一言テーマ: {theme}\n"
        "4カット（オープニング・見せ場1・見せ場2・締め）の絵コンテをJSON配列で。"
        "各要素は scene_desc / image_prompt / motion_prompt / narration を持つこと。"
    )
    try:
        if not llm.is_available():
            raise RuntimeError("LLMサーバに未接続")
        cuts = _parse_cuts(llm.chat(prompt, system=_PLANNER_SYSTEM, temperature=0.6))
        if not cuts:
            raise ValueError("空の絵コンテ")
        source = "LLM"
    except Exception as e:  # 接続不可/パース失敗などはモックで継続
        cuts = _mock_cuts(award, theme)
        source = f"mock（{type(e).__name__}）"

    return {"storyboard": cuts,
            **_log(state, "planner", f"{len(cuts)}カット生成 / {award} / source={source}")}


# --- 2. Asset（統合: カタログ照合＋実写/生成の振り分け）--------------------
def asset(state: AgentState) -> dict[str, Any]:
    """北九州パレットのカタログと照合し、各カットに実写/生成を割り当てる。

    旧「Asset Planner」と「Asset Ingest」を統合。カタログ（asset_catalog.json）が
    あれば賞に合うジャンルの実写を探して採用、無ければ生成に倒す。
    ※ 実写候補の"選定"は将来キュレーター/プロデューサー・エージェントに置換予定。
      現状はタグ一致による決定的な暫定ロジック。
    """
    award = state.get("target_award", "観光賞")
    cuts: list[Cut] = state["storyboard"]
    catalog = assets.load_catalog()
    target_genre = assets.AWARD_GENRE.get(award)

    used: set[str] = set()
    n_official = 0
    for c in cuts:
        pick = assets.pick_for_cut(catalog, target_genre, used) if catalog else None
        if pick:
            c.source = "official_photo"
            c.asset_title = pick["title"]
            c.asset_url = pick["image_url"]
            used.add(pick["detail_url"])
            # 実ファイルを assets_dl/ に保存し、image_path に実パスを入れる
            local = assets.download_image(pick["image_url"])
            c.image_path = local  # DL失敗時は None（asset_url は保持）
            n_official += 1
        else:
            c.source = "generate"

    n_dl = sum(1 for c in cuts if c.source == "official_photo" and c.image_path)
    have = "あり" if catalog else "なし（全カット生成）"
    msg = (f"カタログ={have} / 実写採用={n_official}件（DL成功{n_dl}件）"
           f" / 生成={len(cuts) - n_official}件")
    return {"storyboard": cuts, **_log(state, "asset", msg)}


# --- 3. Image Gen ---------------------------------------------------------
def image_gen(state: AgentState) -> dict[str, Any]:
    backend = state.get("config", {}).get("backend", "local")
    img, _, _ = get_backends(backend)
    cuts: list[Cut] = state["storyboard"]
    n_gen = 0
    for c in cuts:
        # 公式実写のカットは生成しない（素材をそのまま使う）
        if c.source != "generate":
            continue
        # 未合格のカットだけ生成/再生成（QAループ）
        if c.qa_ok:
            continue
        c.image_path = img.generate_image(c.image_prompt, f"work/img_{c.id}.txt")
        n_gen += 1
    return {"storyboard": cuts, **_log(state, "image_gen", f"生成 {n_gen}カット（実写はスキップ）")}


# --- 4. Critic / QA  (チェックポイント) -----------------------------------
def qa(state: AgentState) -> dict[str, Any]:
    cfg = state.get("config", {})
    cuts: list[Cut] = state["storyboard"]
    # 実装: VLM で画像とpromptの整合を判定
    for c in cuts:
        if c.qa_ok:
            continue
        # 公式実写は生成物ではないので自動合格
        if c.source != "generate":
            c.qa_ok = True
            c.qa_reason = "公式素材（QA対象外）"
            continue
        # モック: 生成カットの cut3 は初回だけ NG にしてリトライを発火させる
        if c.id == 3 and c.retries == 0:
            c.qa_ok = False
            c.qa_reason = "構図がpromptと不一致"
            c.retries += 1
        else:
            c.qa_ok = True
            c.qa_reason = "OK"

    ng = [c.id for c in cuts if not c.qa_ok]
    update = {"storyboard": cuts, **_log(state, "qa", f"NG={ng}")}

    # チェックポイント: 有効かつ全カット判定済みなら人の承認を待つ
    if cfg.get("checkpoints", {}).get("qa") and not ng:
        decision = interrupt({"node": "qa", "summary": "全カットQA通過。続行しますか？"})
        update["log"] = update["log"] + [{"node": "qa", "msg": f"checkpoint決定={decision}"}]
    return update


def qa_router(state: AgentState) -> str:
    """未合格が残り、リトライ上限内なら image_gen へ戻す。"""
    cfg = state.get("config", {})
    max_retries = cfg.get("max_retries", 2)
    cuts: list[Cut] = state["storyboard"]
    for c in cuts:
        if not c.qa_ok and c.retries <= max_retries:
            return "retry"
    return "continue"


# --- 5. Video Gen ---------------------------------------------------------
def video_gen(state: AgentState) -> dict[str, Any]:
    backend = state.get("config", {}).get("backend", "local")
    _, vid, _ = get_backends(backend)
    cuts: list[Cut] = state["storyboard"]
    for c in cuts:
        c.video_path = vid.image_to_video(c.image_path, c.motion_prompt,
                                          c.seconds, f"work/vid_{c.id}.txt")
    return {"storyboard": cuts, **_log(state, "video_gen", "画像→動画")}


# --- 6. Audio -------------------------------------------------------------
def audio(state: AgentState) -> dict[str, Any]:
    backend = state.get("config", {}).get("backend", "local")
    _, _, aud = get_backends(backend)
    cuts: list[Cut] = state["storyboard"]
    narration = " ".join(c.narration for c in cuts)
    audio_paths = {
        "narration_path": aud.tts(narration, "work/narration.txt"),
        "bgm_path": aud.bgm(f"{state.get('target_award','')} BGM", "work/bgm.txt"),
    }
    return {"audio": audio_paths, **_log(state, "audio", "ナレーション+BGM")}


# --- 7. Assembly  (チェックポイント) --------------------------------------
def assembly(state: AgentState) -> dict[str, Any]:
    cfg = state.get("config", {})
    # 実装: FFmpeg で結合・字幕・BGMミックス
    final = "work/final.mp4"
    update = {"final_video": final, **_log(state, "assembly", "結合→final.mp4")}
    if cfg.get("checkpoints", {}).get("assembly"):
        decision = interrupt({"node": "assembly", "summary": "動画完成。提出用に確定しますか？"})
        update["log"] = update["log"] + [{"node": "assembly", "msg": f"checkpoint決定={decision}"}]
    return update


# --- 8. Provenance --------------------------------------------------------
def provenance(state: AgentState) -> dict[str, Any]:
    import json
    from pathlib import Path

    cuts: list[Cut] = state.get("storyboard", [])
    record = {
        "target_award": state.get("target_award"),
        "theme": state.get("theme"),
        "config": state.get("config"),
        "cuts": [vars(c) for c in cuts],
        "audio": state.get("audio"),
        "final_video": state.get("final_video"),
        "log": state.get("log", []),
    }
    Path("work").mkdir(parents=True, exist_ok=True)
    Path("work/workflow_log.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return _log(state, "provenance", "workflow_log.json 出力")
