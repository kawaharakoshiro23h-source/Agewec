"""レポート末尾の「実行サマリー」の集計ロジックを検証する。

審査側へ提示する数値（費用・時間・人間の介入）なので、
- 見積ではなく実課金を出す
- 全run累積の台帳ではなく、この run の分だけ数える
- 記録が欠けているときに 0 や誤った数字を出さない
を守れているかを確認する。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agewec_v2.pipeline_runtime import (
    _human_intervention_summary,
    _llm_usage_totals,
    _run_summary_html,
    _video_cost_summary,
)


def _state(work_dir: str, **overrides):
    state = {
        "run_id": "run-summary-test",
        "config": {
            "paths": {"work_dir": work_dir},
            "llm": {
                "model": "gpt-4o-mini",
                "cost_guard": {
                    "input_cost_per_million_usd": 0.15,
                    "output_cost_per_million_usd": 0.60,
                },
            },
        },
        "phase_results": {},
        "phase_timings": {},
        "reviews": [],
        "cut_results": {},
    }
    state.update(overrides)
    return state


class LLMUsageTotalsTest(unittest.TestCase):
    def _with_usage(self, work_dir):
        return _state(
            work_dir,
            phase_results={
                "executive_producer": {
                    "llm": {"usage": {
                        "prompt_tokens": 1000, "completion_tokens": 500,
                    }}
                },
                "director": {
                    "llm": {"usage": {
                        "prompt_tokens": 2000, "completion_tokens": 1000,
                    }}
                },
                # LLMを使わない決定的ノードは数えない
                "post_production": {"status": "success"},
            },
        )

    def test_sums_tokens_across_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            totals = _llm_usage_totals(self._with_usage(tmp))
        self.assertEqual(totals["prompt_tokens"], 3000)
        self.assertEqual(totals["completion_tokens"], 1500)
        self.assertEqual(totals["total_tokens"], 4500)
        self.assertEqual(totals["calls"], 2)

    def test_cost_uses_configured_per_million_rates(self) -> None:
        """入力と出力で単価が違うため、合算トークン×単価では誤る。"""
        with tempfile.TemporaryDirectory() as tmp:
            totals = _llm_usage_totals(self._with_usage(tmp))
        expected = (3000 * 0.15 + 1500 * 0.60) / 1_000_000
        self.assertAlmostEqual(totals["cost_usd"], round(expected, 6))

    def test_masked_usage_is_reported_not_counted_as_zero(self) -> None:
        """古いrunでは usage が "***" に伏せられている。

        0トークン・$0 と表示すると「無料で動いた」と誤読されるため、
        利用不可であることを available/masked で示す。
        """
        with tempfile.TemporaryDirectory() as tmp:
            totals = _llm_usage_totals(_state(tmp, phase_results={
                "director": {"llm": {"usage": {
                    "prompt_tokens": "***", "completion_tokens": "***",
                }}}
            }))
        self.assertFalse(totals["available"])
        self.assertTrue(totals["masked"])


class VideoCostSummaryTest(unittest.TestCase):
    def test_reads_actual_charges_from_run_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(tmp)
            ledger = Path(tmp) / "runs" / "run-summary-test"
            ledger.mkdir(parents=True)
            (ledger / "video_cost_ledger.json").write_text(json.dumps({
                "spent_usd": 1.2,
                "estimated_usd": 1.0,
                "generations": [
                    {"cut_id": 1, "model": "gen4.5",
                     "billed_seconds": 5.0, "cost_usd": 0.6},
                    {"cut_id": 2, "model": "gen4.5",
                     "billed_seconds": 5.0, "cost_usd": 0.6},
                ],
            }), encoding="utf-8")
            summary = _video_cost_summary(state)
        # 見積(1.0)ではなく実課金(1.2)を採る
        self.assertAlmostEqual(summary["spent_usd"], 1.2)
        self.assertEqual(len(summary["generations"]), 2)

    def test_missing_ledger_is_zero_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = _video_cost_summary(_state(tmp))
        self.assertEqual(summary["spent_usd"], 0.0)
        self.assertEqual(summary["generations"], [])


class HumanInterventionTest(unittest.TestCase):
    def test_counts_only_human_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counts = _human_intervention_summary(_state(tmp, reviews=[
                {"action": "approve", "decided_by": "human"},
                {"action": "approve", "decided_by": "human"},
                {"action": "retry_with_feedback", "decided_by": "human"},
                # ポリシーによる自動承認は「人間の介入」ではない
                {"action": "approve", "decided_by": "policy"},
            ]))
        self.assertEqual(counts["approve"], 2)
        self.assertEqual(counts["retry_with_feedback"], 1)

    def test_counts_human_override_of_ai_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counts = _human_intervention_summary(_state(tmp, cut_results={
                "1": {"qa": {"decided_by": "human"}},
                "2": {"qa": {"decided_by": "ai"}},
            }))
        self.assertEqual(counts["override"], 1)


class RunSummaryHtmlTest(unittest.TestCase):
    def _html(self, tmp: str) -> str:
        state = _state(
            tmp,
            phase_results={"director": {"llm": {"usage": {
                "prompt_tokens": 1000, "completion_tokens": 500,
            }}}},
            phase_timings={"director": {
                "phase": "director", "runs": 2,
                "cumulative_duration_seconds": 13.0,
                "last_status": "success",
            }},
            reviews=[{"action": "approve", "decided_by": "human"}],
        )
        ledger = Path(tmp) / "runs" / "run-summary-test"
        ledger.mkdir(parents=True)
        (ledger / "video_cost_ledger.json").write_text(json.dumps({
            "spent_usd": 1.2,
            "generations": [{"cut_id": 1, "model": "gen4.5",
                             "billed_seconds": 5.0, "cost_usd": 0.6,
                             "job_id": "abcdef123456"}],
        }), encoding="utf-8")
        return _run_summary_html(state)

    def test_total_cost_combines_llm_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html = self._html(tmp)
        # LLM 0.00045 + 動画 1.20 → 表示は $1.20
        self.assertIn("合計 $1.20", html)

    def test_shows_phase_and_video_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html = self._html(tmp)
        self.assertIn("実行サマリー", html)
        self.assertIn("工程別の所要時間", html)
        self.assertIn("動画生成の実課金", html)
        self.assertIn("gen4.5", html)
        self.assertIn("abcdef12", html)          # job_idは先頭8文字
        self.assertIn("Director", html)          # phase名は日本語タイトルへ

    def test_phase_table_follows_flow_order_not_duration(self) -> None:
        """工程は実行順（01→02→…）で並べる。所要時間の降順では読みにくい。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(tmp, phase_timings={
                # 遅い順に並べると post_production が先頭に来る配置
                "post_production": {
                    "phase": "post_production", "runs": 1,
                    "cumulative_duration_seconds": 300.0,
                    "last_status": "success",
                },
                "executive_producer": {
                    "phase": "executive_producer", "runs": 1,
                    "cumulative_duration_seconds": 1.0,
                    "last_status": "success",
                },
                "commit_cut_qa": {            # 図に無い内部ノードは末尾
                    "phase": "commit_cut_qa", "runs": 1,
                    "cumulative_duration_seconds": 0.5,
                    "last_status": "success",
                },
            })
            html = _run_summary_html(state)
        body = html[html.index("工程別の所要時間"):html.index("動画生成の実課金")]
        positions = [
            body.index("Executive Producer"),
            body.index("Post Production"),
            body.index("カット判定の確定"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_states_that_wait_time_is_excluded(self) -> None:
        """「総所要時間」を人間の待ち時間込みと誤解されないようにする。"""
        with tempfile.TemporaryDirectory() as tmp:
            html = self._html(tmp)
        self.assertIn("承認画面での待ち時間は含みません", html)


if __name__ == "__main__":
    unittest.main()
