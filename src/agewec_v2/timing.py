"""フェーズ別の実行時間計測。

【本番経路: 現役】graph_safe が各ノードを登録する際に、この共通ラッパーで囲む。

`_complete()` への追記ではなく **ノードの入口と出口** を包むことで、次を正確に測る。

- 例外で終了したフェーズ（`_complete` を通らない）
- 再試行（同じフェーズが複数回実行される）
- 実処理に加えたガード・レビュー往復を除いた純粋な処理時間

記録先:
  1. `state["phase_timings"][phase]` … 累積・試行回数を含む集計
  2. `phase_results[phase]["timing"]` … そのフェーズ結果に紐づく計測値
  3. `state["events"]` … `phase_timed` イベント（時系列で追える）

既存の計測（LLM の elapsed / ComfyUI の elapsed_seconds / 全体の started_at）とは
競合せず、フェーズ単位の粒度を追加する。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(
    state: dict[str, Any],
    phase: str,
    *,
    started_at: float,
    started_iso: str,
    duration: float,
    status: str,
) -> dict[str, Any]:
    """phase_timings の新しい値を返す（stateは書き換えない）。"""
    timings = dict(state.get("phase_timings", {}))
    previous = dict(timings.get(phase, {}))
    runs = int(previous.get("runs", 0)) + 1
    cumulative = round(
        float(previous.get("cumulative_duration_seconds", 0.0)) + duration, 3
    )
    timings[phase] = {
        "phase": phase,
        "attempt": runs,
        "runs": runs,
        "started_at": round(started_at, 3),
        "started_at_iso": started_iso,
        "duration_seconds": duration,
        "cumulative_duration_seconds": cumulative,
        "last_status": status,
    }
    return timings


def with_phase_timing(phase: str, function: Callable) -> Callable:
    """ノード関数を包み、入口〜出口の所要時間を記録する。

    正常終了時は返り値の update に `phase_timings` と、可能なら
    `phase_results[phase]["timing"]` を差し込む。
    例外時も計測結果をイベントとして残せるよう、例外に情報を添えて送出する。
    """

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        started_at = time.time()
        started_iso = _now_iso()
        try:
            update = function(state)
        except Exception as exc:  # noqa: BLE001 - 計測後に再送出する
            duration = round(time.time() - started_at, 3)
            # 例外は上位（Execution Guard / Review Gate）で処理させる。
            # 計測値は失われないよう例外へ添付しておく。
            setattr(exc, "agewec_phase_timing", {
                "phase": phase,
                "started_at_iso": started_iso,
                "duration_seconds": duration,
                "status": "error",
            })
            raise

        duration = round(time.time() - started_at, 3)
        if not isinstance(update, dict):
            return update

        result = update.get("phase_results", {}).get(phase)
        status = (result or {}).get("status", "unknown")
        timings = _record(
            state,
            phase,
            started_at=started_at,
            started_iso=started_iso,
            duration=duration,
            status=status,
        )
        merged = dict(update)
        merged["phase_timings"] = timings

        # フェーズ結果にも計測値を載せる（レポート・provenanceで参照しやすくする）
        if result is not None:
            phase_results = dict(merged["phase_results"])
            enriched = dict(result)
            enriched["timing"] = {
                "started_at_iso": started_iso,
                "duration_seconds": duration,
                "attempt": timings[phase]["attempt"],
                "cumulative_duration_seconds":
                    timings[phase]["cumulative_duration_seconds"],
            }
            phase_results[phase] = enriched
            merged["phase_results"] = phase_results

        events = list(merged.get("events", state.get("events", [])))
        events.append(
            {
                "t": round(time.time(), 3),
                "type": "phase_timed",
                "phase": phase,
                "duration_seconds": duration,
                "attempt": timings[phase]["attempt"],
                "cumulative_duration_seconds":
                    timings[phase]["cumulative_duration_seconds"],
                "status": status,
            }
        )
        merged["events"] = events
        return merged

    wrapped.__name__ = getattr(function, "__name__", phase)
    wrapped.__doc__ = getattr(function, "__doc__", None)
    return wrapped


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """実行全体のフェーズ別時間サマリを返す（レポート用）。"""
    timings = state.get("phase_timings", {})
    rows = sorted(
        timings.values(),
        key=lambda item: item.get("cumulative_duration_seconds", 0.0),
        reverse=True,
    )
    total = round(
        sum(float(r.get("cumulative_duration_seconds", 0.0)) for r in rows), 3
    )
    return {
        "total_phase_seconds": total,
        "slowest_phase": rows[0]["phase"] if rows else None,
        "phases": rows,
    }
