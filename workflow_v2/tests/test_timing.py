"""フェーズ別タイマーの回帰テスト。"""
from __future__ import annotations

import time
import unittest

from agewec_v2.timing import summarize, with_phase_timing


def _node(state):
    """_complete と同じ形の update を返す簡易ノード。"""
    time.sleep(0.02)
    return {
        "phase_results": {
            "demo": {"phase": "demo", "status": "success", "summary": "ok"}
        },
        "events": list(state.get("events", [])),
    }


class PhaseTimingTest(unittest.TestCase):
    def test_records_duration_and_attempt(self) -> None:
        wrapped = with_phase_timing("demo", _node)
        update = wrapped({})
        timing = update["phase_timings"]["demo"]
        self.assertGreater(timing["duration_seconds"], 0)
        self.assertEqual(timing["attempt"], 1)
        self.assertEqual(timing["last_status"], "success")
        self.assertIn("started_at_iso", timing)

    def test_injects_timing_into_phase_result(self) -> None:
        wrapped = with_phase_timing("demo", _node)
        update = wrapped({})
        result_timing = update["phase_results"]["demo"]["timing"]
        self.assertGreater(result_timing["duration_seconds"], 0)
        self.assertEqual(result_timing["attempt"], 1)

    def test_accumulates_across_retries(self) -> None:
        """再試行しても累積時間と試行回数が積み上がる。"""
        wrapped = with_phase_timing("demo", _node)
        state: dict = {}
        first = wrapped(state)
        state["phase_timings"] = first["phase_timings"]
        second = wrapped(state)
        timing = second["phase_timings"]["demo"]
        self.assertEqual(timing["attempt"], 2)
        self.assertEqual(timing["runs"], 2)
        self.assertGreater(
            timing["cumulative_duration_seconds"],
            timing["duration_seconds"],
        )

    def test_emits_phase_timed_event(self) -> None:
        wrapped = with_phase_timing("demo", _node)
        update = wrapped({})
        kinds = [e.get("type") for e in update["events"]]
        self.assertIn("phase_timed", kinds)

    def test_exception_is_measured_and_reraised(self) -> None:
        """例外で終わっても計測値が例外に添付され、例外自体は伝播する。"""

        def failing(_state):
            time.sleep(0.01)
            raise RuntimeError("boom")

        wrapped = with_phase_timing("demo", failing)
        with self.assertRaises(RuntimeError) as ctx:
            wrapped({})
        measured = getattr(ctx.exception, "agewec_phase_timing", None)
        self.assertIsNotNone(measured)
        self.assertEqual(measured["status"], "error")
        self.assertGreater(measured["duration_seconds"], 0)

    def test_summarize_orders_by_cumulative_time(self) -> None:
        state = {
            "phase_timings": {
                "fast": {"phase": "fast", "cumulative_duration_seconds": 1.0},
                "slow": {"phase": "slow", "cumulative_duration_seconds": 9.0},
            }
        }
        summary = summarize(state)
        self.assertEqual(summary["slowest_phase"], "slow")
        self.assertAlmostEqual(summary["total_phase_seconds"], 10.0)


if __name__ == "__main__":
    unittest.main()
