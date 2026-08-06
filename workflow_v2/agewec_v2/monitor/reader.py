"""実行中のrunを `work/runs/<run_id>/` から読み取る。

このモジュールはパイプラインに一切干渉しない。ディスクへ書かれた成果物を
外から観測するだけで、状態を書き換えることも、実行中プロセスへ通知することも
しない。したがって監視を起動・停止しても本番の挙動は変わらない。

なぜファイル監視で足りるか:
    パイプラインは各工程の完了時にゲートJSON・カット別リクエスト・生成動画・
    課金台帳を逐次書き出す。ファイルの mtime がそのまま「いつ何が起きたか」に
    なるため、実行中でも進行状況を復元できる。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 工程の並びと表示名は本番のレポートと同じ定義を使う（二重管理を避ける）。
from ..pipeline_runtime import _PHASE_PRESENTATION

_GATE_NAME = re.compile(r"^(?P<phase>.+)_attempt_(?P<attempt>\d+)\.json$")
_CUT_DIR = re.compile(r"^cut_(?P<cut_id>\d+)$")
_ATTEMPT_REQUEST = re.compile(r"^attempt_(?P<attempt>\d+)_request\.json$")
# 生成が始まってからこの秒数を超えて音沙汰がなければ「実行中」表示を弱める
_STALE_SECONDS = 900.0
# 映像生成はゲートを書かない（review policy が never）。カット成果物の
# 出方から進行を読むため、フェーズIDを定数にしておく。
_PRODUCTION_PHASE = "image_video_production"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def list_runs(runs_root: Path) -> list[dict[str, Any]]:
    """新しい順に run の一覧を返す。

    更新時刻はゲートJSONとカット成果物だけから求める。run配下を全走査すると、
    動画を多数含むディレクトリが並ぶ環境で目に見えて遅くなるため。
    `has_gates` は「パイプラインが実際に流れた run か」の目印で、古い実験用
    ディレクトリを既定の表示対象にしないために使う。
    """
    if not runs_root.is_dir():
        return []
    runs = []
    for directory in runs_root.iterdir():
        if not directory.is_dir():
            continue
        gates = sorted((directory / "gates").glob("*.json"))
        stamps = [m for m in (_mtime(p) for p in gates) if m]
        # 生成直後は動画の方が新しいので、カット側も見る
        for cut_dir in (directory / "cuts").glob("cut_*"):
            stamps.extend(
                m for m in (_mtime(p) for p in cut_dir.iterdir()) if m
            )
        ledger = _mtime(directory / "video_cost_ledger.json")
        if ledger:
            stamps.append(ledger)
        runs.append(
            {
                "run_id": directory.name,
                "updated_at": max(stamps, default=_mtime(directory) or 0.0),
                "has_gates": bool(gates),
            }
        )
    # ゲートのある本物のrunを先に、その中で新しい順
    return sorted(
        runs,
        key=lambda r: (r["has_gates"], r["updated_at"]),
        reverse=True,
    )


# 工程ごとに「開いたとき何を見たいか」。ここに無い工程は data をそのまま出す。
# 巨大な配列（素材候補一覧など）を丸ごと返すと1回のポーリングが重くなるため、
# 表示に使う項目だけを選ぶ。
_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "executive_producer": (
        "objective", "audience", "deliverable", "target_duration_seconds",
        "target_award", "constraints", "success_criteria",
    ),
    "creative_director": (
        "title", "logline", "tone", "visual_language", "camera_intent",
        "audio_direction",
    ),
    "writer_storyboard": ("total_seconds", "cuts"),
    "asset_curator": ("asset_assignments",),
    "director": ("shots", "continuity_checks"),
    "visual_qa": ("verdict", "issues", "route"),
    "post_production": (
        "implementation", "output_path", "technical_qa", "operations",
    ),
    "review_board": (
        "mode", "verdict", "average", "rubric_scores", "recommendations",
    ),
}
# 素材候補は1カットあたり数十件になることがあるので、先頭だけを見せる
_MAX_LIST_ITEMS = 12


def _trim(value: Any) -> Any:
    """長すぎる配列を切り詰める（何件省いたかは画面に出す）。"""
    if isinstance(value, list) and len(value) > _MAX_LIST_ITEMS:
        return value[:_MAX_LIST_ITEMS] + [
            {"_truncated": len(value) - _MAX_LIST_ITEMS}
        ]
    return value


def _with_asset_previews(assignments: Any) -> Any:
    """素材選定に、原本を見に行かずに済むサムネイルURLを添える。

    素材の実体は `assets_dl/` にあり、run配下ではないため `/media/` では
    配信できない（run外を出さない安全策のため）。代わりに専用の
    `/asset/<ファイル名>` を使う。原本は最大20MB規模なので、サーバ側で
    縮小してから返す。
    """
    if not isinstance(assignments, list):
        return assignments
    result = []
    for item in assignments:
        if not isinstance(item, dict) or item.get("_truncated"):
            result.append(item)
            continue
        entry = dict(item)
        for key in ("primary", "alternatives"):
            value = entry.get(key)
            choices = value if isinstance(value, list) else [value]
            annotated = []
            for choice in choices:
                if not isinstance(choice, dict):
                    annotated.append(choice)
                    continue
                name = Path(str(choice.get("local_path") or "")).name
                annotated.append(
                    {**choice, "preview_url": f"/asset/{name}" if name else None}
                )
            entry[key] = annotated if isinstance(value, list) else annotated[0]
        result.append(entry)
    return result


def _phase_output(phase: str, gate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not gate:
        return None
    data = gate.get("data")
    if not isinstance(data, dict):
        return {"value": data} if data is not None else None
    fields = _OUTPUT_FIELDS.get(phase)
    selected = (
        {key: value for key, value in data.items()}
        if not fields
        else {key: data[key] for key in fields if key in data}
    )
    output = {key: _trim(value) for key, value in selected.items()}
    if "asset_assignments" in output:
        output["asset_assignments"] = _with_asset_previews(
            output["asset_assignments"]
        )
    # Director も素材を持つので、同じくサムネイルを添える
    if isinstance(output.get("shots"), list):
        output["shots"] = [
            {
                **shot,
                "asset": {
                    **shot["asset"],
                    "preview_url": (
                        f"/asset/{Path(str(shot['asset'].get('local_path') or '')).name}"
                        if (shot.get("asset") or {}).get("local_path")
                        else None
                    ),
                },
            }
            if isinstance(shot, dict) and isinstance(shot.get("asset"), dict)
            else shot
            for shot in output["shots"]
        ]
    return output


def _phase_progress(run_dir: Path) -> list[dict[str, Any]]:
    """工程ごとの実行回数・所要時間・最新の要約を組み立てる。

    所要時間は「前の工程のゲートが書かれてから、この工程のゲートが書かれるまで」
    で近似する。人間が承認画面で考えていた時間も含まれるため、レポートの
    `timing_report.json`（処理時間のみ）とは意味が異なる。実行中は
    timing がまだ書かれないので、こちらでしか進行を追えない。
    """
    gates_dir = run_dir / "gates"
    found: dict[str, list[dict[str, Any]]] = {}
    if gates_dir.is_dir():
        for path in gates_dir.iterdir():
            match = _GATE_NAME.match(path.name)
            if not match:
                continue
            found.setdefault(match["phase"], []).append(
                {
                    "attempt": int(match["attempt"]),
                    "at": _mtime(path) or 0.0,
                    "path": path,
                }
            )

    # 全ゲートを時刻順に並べ、直前のゲートとの差を所要時間とみなす
    ordered = sorted(
        (entry for entries in found.values() for entry in entries),
        key=lambda e: e["at"],
    )
    started_at = min((e["at"] for e in ordered), default=None)
    elapsed_by_path: dict[Path, float] = {}
    previous = started_at
    for entry in ordered:
        if previous is not None:
            elapsed_by_path[entry["path"]] = max(0.0, entry["at"] - previous)
        previous = entry["at"]

    phases = []
    for guide in _PHASE_PRESENTATION:
        entries = sorted(
            found.get(guide["id"], []), key=lambda e: e["attempt"]
        )
        latest = entries[-1] if entries else None
        gate = _load_json(latest["path"]) if latest else None
        phases.append(
            {
                "id": guide["id"],
                "number": guide["number"],
                "title": guide["title"],
                "kind": guide["kind"],
                "runs": len(entries),
                "status": (gate or {}).get("status") if gate else "pending",
                "summary": (gate or {}).get("summary") if gate else None,
                "blocking_issues": (gate or {}).get("blocking_issues") or [],
                # 画面で「この工程の成果物を見る」を押したときに開く中身。
                # ゲートJSONの data がそのまま工程の出力なので、加工せず渡し、
                # 見せ方は画面側に任せる（工程が増えてもreaderを直さずに済む）。
                "output": _phase_output(guide["id"], gate),
                "feedback": {
                    "received": (gate or {}).get("feedback_received") or "",
                    "origin": (gate or {}).get("feedback_origin"),
                    "status": (gate or {}).get("feedback_status"),
                } if gate else None,
                "finished_at": latest["at"] if latest else None,
                "elapsed_seconds": (
                    round(elapsed_by_path.get(latest["path"], 0.0), 1)
                    if latest
                    else None
                ),
            }
        )
    return phases


def _cuts(run_dir: Path) -> list[dict[str, Any]]:
    """カットごとの入力画像・生成動画・QA結果・試行回数を集める。"""
    cuts_dir = run_dir / "cuts"
    if not cuts_dir.is_dir():
        return []
    results = []
    for directory in sorted(cuts_dir.iterdir()):
        match = _CUT_DIR.match(directory.name)
        if not match:
            continue
        cut_id = int(match["cut_id"])
        request = _load_json(directory / "request.json") or {}
        qa = _load_json(directory / "qa.json") or {}
        # 生成物は attempt 番号の大きいものが最新
        clips = sorted(directory.glob("attempt_*.mp4"))
        # 承認記録だけが置かれた副産物ディレクトリ（cut_00 など）は、
        # 表示できる情報が何もないので一覧に出さない。生成待ちのカットは
        # request.json を持つため、ここで消えることはない。
        if not request and not clips and not qa:
            continue
        errors = sorted(directory.glob("attempt_*_error.json"))
        latest_error = _load_json(errors[-1]) if errors else None
        source = next(
            (p for p in directory.glob("source.*") if p.is_file()), None
        )
        results.append(
            {
                "cut_id": cut_id,
                "generation_mode": request.get("generation_mode")
                or "image_to_video",
                "model": request.get("model"),
                "seconds": request.get("actual_seconds")
                or request.get("requested_seconds"),
                "seed": request.get("seed"),
                "prompt": request.get("positive_prompt"),
                "camera_motion": request.get("camera_motion"),
                "attempt": request.get("attempt"),
                "source_url": (
                    f"/media/{run_dir.name}/cuts/{directory.name}/{source.name}"
                    if source
                    else None
                ),
                "clip_url": (
                    f"/media/{run_dir.name}/cuts/{directory.name}/{clips[-1].name}"
                    if clips
                    else None
                ),
                "clip_at": _mtime(clips[-1]) if clips else None,
                "verdict": qa.get("verdict"),
                "issues": [
                    item.get("description")
                    for item in qa.get("issues", [])
                    if isinstance(item, dict)
                ],
                "error": (
                    (latest_error or {}).get("error")
                    or (latest_error or {}).get("message")
                    if latest_error
                    else None
                ),
            }
        )
    return results


def _cost(run_dir: Path) -> dict[str, Any]:
    ledger = _load_json(run_dir / "video_cost_ledger.json") or {}
    return {
        "spent_usd": float(ledger.get("spent_usd", 0.0)),
        "generations": ledger.get("generations", []),
    }


def _generating_now(run_dir: Path, now: float) -> dict[str, Any] | None:
    """外部APIで生成中のカットを特定する。

    生成は `attempt_NN_request.json` を書いてから始まり、完了時に
    `attempt_NN.mp4` が現れる。失敗時は `attempt_NN_error.json` が出る。
    したがって「requestがあり、対応するmp4もerrorも無い試行」が
    まさに生成中の1件になる。映像生成はゲートを書かないため、
    ここを見ないと数分間なにも起きていないように見えてしまう。
    """
    cuts_dir = run_dir / "cuts"
    if not cuts_dir.is_dir():
        return None

    # run 全体で最後に書かれた時刻。生成要求より新しい書き込みがあるなら、
    # パイプラインはその要求より先へ進んでいる（＝もう生成中ではない）。
    # 差し戻しで破棄された試行は request だけが残り mp4 も error も
    # 作られないため、この比較が無いと永久に「生成中」と表示されてしまう。
    newest = 0.0
    for path in (run_dir / "gates").glob("*.json"):
        newest = max(newest, _mtime(path) or 0.0)
    for cut_dir in cuts_dir.glob("cut_*"):
        for path in cut_dir.iterdir():
            if _ATTEMPT_REQUEST.match(path.name) or path.name == "request.json":
                continue          # 要求そのものは「進んだ証拠」にしない
            newest = max(newest, _mtime(path) or 0.0)

    pending: list[dict[str, Any]] = []
    for directory in sorted(cuts_dir.iterdir()):
        match = _CUT_DIR.match(directory.name)
        if not match:
            continue
        for path in directory.iterdir():
            attempt_match = _ATTEMPT_REQUEST.match(path.name)
            if not attempt_match:
                continue
            attempt = attempt_match["attempt"]
            done = directory / f"attempt_{attempt}.mp4"
            failed = directory / f"attempt_{attempt}_error.json"
            if done.exists() or failed.exists():
                continue
            started = _mtime(path) or now
            if started < newest:
                continue          # 後続の書き込みに追い越された＝破棄された試行
            pending.append(
                {
                    "cut_id": int(match["cut_id"]),
                    "attempt": int(attempt),
                    "started_at": started,
                    "elapsed_seconds": round(max(0.0, now - started), 1),
                }
            )
    if not pending:
        return None
    # 同時に複数走ることは無い設計。最新のものを現在地とする。
    return max(pending, key=lambda item: item["started_at"])


def _fill_production_phase(
    run_dir: Path,
    phases: list[dict[str, Any]],
    cuts: list[dict[str, Any]],
) -> None:
    """映像生成の行を、カット成果物から埋める。

    この工程は review policy が `never` なのでゲートJSONが作られない。
    そのままでは「一度も実行されていない」ように見えるが、実際には最も
    時間と費用がかかる工程なので、mp4 とエラーJSONから実績を復元する。
    """
    phase = next(
        (p for p in phases if p["id"] == _PRODUCTION_PHASE), None
    )
    if phase is None or phase["finished_at"]:
        return
    cuts_dir = run_dir / "cuts"
    clips = sorted(cuts_dir.glob("cut_*/attempt_*.mp4")) if cuts_dir.is_dir() else []
    errors = (
        sorted(cuts_dir.glob("cut_*/attempt_*_error.json"))
        if cuts_dir.is_dir()
        else []
    )
    if not clips and not errors:
        return
    stamps = [m for m in (_mtime(p) for p in clips + errors) if m]
    generated = [c for c in cuts if c.get("clip_url")]
    phase.update(
        {
            "runs": len(clips) + len(errors),
            "status": "error" if errors and not clips else "success",
            "summary": (
                f"{len(generated)}カットを生成"
                + (f" / 失敗 {len(errors)}件" if errors else "")
            ),
            "finished_at": max(stamps, default=None),
            "output": {
                "generated_cuts": [
                    {
                        "cut_id": c["cut_id"],
                        "model": c.get("model"),
                        "seconds": c.get("seconds"),
                        "seed": c.get("seed"),
                        "attempt": c.get("attempt"),
                        "generation_mode": c.get("generation_mode"),
                        "verdict": c.get("verdict"),
                        "error": c.get("error"),
                    }
                    for c in cuts
                ],
                "note": (
                    "この工程は承認ゲートを持たないため、"
                    "生成物から実績を復元して表示しています。"
                ),
            },
        }
    )


def _current_activity(
    run_dir: Path,
    phases: list[dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    """「いま何をしている最中か」を、状態を3つに分けて返す。

        generating … 外部APIで映像を生成中（カット番号と経過秒がわかる）
        waiting    … ゲートが書かれ、CLIが人間の承認入力を待っている
        idle       … 直前の工程から時間が空いている（中断・完了・停止）

    ゲートJSONは工程の完了時に書かれるため、これだけでは「次に何をして
    いるか」が分からない。カット成果物の出方を併せて見ることで、
    数分かかる生成中も「動いている」と示せるようにする。
    """
    finished = [p for p in phases if p["finished_at"]]
    generating = _generating_now(run_dir, now)
    if generating:
        return {
            "state": "generating",
            "phase": _PRODUCTION_PHASE,
            "number": "06",
            "title": "Image / Video Production（映像生成）",
            "cut_id": generating["cut_id"],
            "attempt": generating["attempt"],
            "elapsed_seconds": generating["elapsed_seconds"],
            "stale": generating["elapsed_seconds"] > _STALE_SECONDS,
        }
    if not finished:
        return {"state": "idle", "phase": None, "elapsed_seconds": None,
                "stale": False, "last_update_at": None}
    latest = max(finished, key=lambda p: p["finished_at"])
    idle = max(0.0, now - latest["finished_at"])
    started = min(p["finished_at"] for p in finished)

    # 提出Packageまで到達していれば、この run はもう動かない。
    # 「最後の更新からの経過」を出し続けると、終わったrunで数字が
    # 際限なく増えて意味を失うため、完了は別の状態として扱う。
    if (run_dir / "final" / "final_video.mp4").is_file() or any(
        p["id"] == "provenance" and p["finished_at"] for p in finished
    ):
        return {
            "state": "completed",
            "phase": latest["id"],
            "number": latest["number"],
            "title": latest["title"],
            "elapsed_seconds": None,          # 増え続ける値は出さない
            "total_seconds": round(latest["finished_at"] - started, 1),
            "last_update_at": latest["finished_at"],
            "stale": False,
        }

    # 直近にゲートが書かれた＝承認画面が出ている可能性が高い。
    # 断定はできないので、時間が経つほど idle 寄りに倒す。
    waiting = idle <= _STALE_SECONDS
    return {
        "state": "waiting" if waiting else "idle",
        "phase": latest["id"],
        "number": latest["number"],
        "title": latest["title"],
        # 停止扱いになったら経過を出さず、最終更新時刻だけを示す
        "elapsed_seconds": round(idle, 1) if waiting else None,
        "total_seconds": round(latest["finished_at"] - started, 1),
        "last_update_at": latest["finished_at"],
        "stale": not waiting,
    }


def _annotate_phase_states(
    phases: list[dict[str, Any]],
    activity: dict[str, Any],
) -> None:
    """各工程に done / active / pending の表示状態を与える（画面用）。"""
    active_id = activity.get("phase") if activity.get("state") in (
        "generating",
        "waiting",
    ) else None
    for phase in phases:
        if phase["finished_at"]:
            phase["state"] = "done"
        elif phase["id"] == active_id:
            phase["state"] = "active"
        else:
            phase["state"] = "pending"
    # 生成中は「完了済みだが、今まさに再実行中」もあり得るので上書きする
    if activity.get("state") == "generating":
        for phase in phases:
            if phase["id"] == _PRODUCTION_PHASE:
                phase["state"] = "active"


def read_run(runs_root: Path, run_id: str, *, now: float) -> dict[str, Any]:
    """1つのrunの現在状態を返す（存在しなければ found=False）。"""
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        return {"found": False, "run_id": run_id}
    phases = _phase_progress(run_dir)
    started = min(
        (p["finished_at"] for p in phases if p["finished_at"]), default=None
    )
    cuts = _cuts(run_dir)
    _fill_production_phase(run_dir, phases, cuts)
    activity = _current_activity(run_dir, phases, now)
    _annotate_phase_states(phases, activity)
    return {
        "found": True,
        "run_id": run_id,
        "started_at": started,
        "now": now,
        "phases": phases,
        "cuts": cuts,
        "cost": _cost(run_dir),
        "activity": activity,
        "final_video_url": (
            f"/media/{run_id}/final/final_video.mp4"
            if (run_dir / "final" / "final_video.mp4").is_file()
            else None
        ),
    }
