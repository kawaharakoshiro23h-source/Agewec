"""8つのノード（モック実装）。

実装段階では各ノードの中身を実モデル/実ツール呼び出しに差し替える。
グラフの配線・状態の受け渡し・チェックポイントの位置はこのままで動く。
"""
from __future__ import annotations

import time
from typing import Any

from langgraph.types import interrupt

from .backends import get_backends
from .state import AgentState, Cut


def _log(state: AgentState, node: str, msg: str) -> dict[str, Any]:
    """証跡ログに1行足すための更新差分を返す。"""
    entry = {"t": round(time.time(), 3), "node": node, "msg": msg}
    return {"log": state.get("log", []) + [entry]}


# --- 1. Planner -----------------------------------------------------------
def planner(state: AgentState) -> dict[str, Any]:
    award = state.get("target_award", "観光賞")
    theme = state.get("theme", "北九州の魅力")
    # 実装: ローカルLLM(LM Studio, OpenAI互換API)で絵コンテを生成
    cuts = [
        Cut(id=1, scene_desc="オープニング", image_prompt=f"{theme} opening, cinematic",
            motion_prompt="slow push in", narration="ようこそ、北九州へ。"),
        Cut(id=2, scene_desc="見せ場1", image_prompt=f"{award} 見せ場1",
            motion_prompt="pan right", narration="工場夜景が水面に揺れる。"),
        Cut(id=3, scene_desc="見せ場2", image_prompt=f"{award} 見せ場2",
            motion_prompt="tilt up", narration="歴史と未来が交差する街。"),
        Cut(id=4, scene_desc="締め", image_prompt=f"{theme} ending",
            motion_prompt="slow zoom out", narration="さあ、会いに行こう。"),
    ]
    return {"storyboard": cuts, **_log(state, "planner", f"{len(cuts)}カット生成 / {award}")}


# --- 2. Asset Planner -----------------------------------------------------
def asset_planner(state: AgentState) -> dict[str, Any]:
    # 実装: 各カットを「生成」か「北九州パレット公式素材」か判断
    return _log(state, "asset_planner", "素材方針を決定（生成/公式素材）")


# --- 3. Image Gen ---------------------------------------------------------
def image_gen(state: AgentState) -> dict[str, Any]:
    backend = state.get("config", {}).get("backend", "local")
    img, _, _ = get_backends(backend)
    cuts: list[Cut] = state["storyboard"]
    for c in cuts:
        # 未合格のカットだけ再生成（QAループ）
        if c.qa_ok:
            continue
        c.image_path = img.generate_image(c.image_prompt, f"work/img_{c.id}.txt")
    return {"storyboard": cuts, **_log(state, "image_gen", "画像生成/再生成")}


# --- 4. Critic / QA  (チェックポイント) -----------------------------------
def qa(state: AgentState) -> dict[str, Any]:
    cfg = state.get("config", {})
    cuts: list[Cut] = state["storyboard"]
    # 実装: VLM で画像とpromptの整合を判定
    for c in cuts:
        if c.qa_ok:
            continue
        # モック: cut3 は初回だけ NG にしてリトライを発火させる
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
