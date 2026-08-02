"""time_of_day の正規化と、昼夜事故の防止に関する回帰テスト。"""
from __future__ import annotations

import unittest

from agewec_v2.nodes_llm import (
    _asset_time_of_day,
    _shortlist_candidates,
    _tod_eval,
    normalize_time_of_day,
)


def _candidate(asset_id: str, title: str, genres=None, areas=None):
    genres = genres or []
    return {
        "asset_id": asset_id,
        "title": title,
        "genres": genres,
        "areas": areas or ["小倉都心部エリア"],
        "local_available": True,
        "local_path": f"assets_dl/{asset_id}.jpg",
        "time_of_day": _asset_time_of_day(title, genres),
    }


class NormalizeTimeOfDayTest(unittest.TestCase):
    def test_japanese_and_english_aliases_map_to_canonical(self) -> None:
        for raw in ("朝", "早朝", "morning", "dawn", "昼", "daytime", "午後"):
            self.assertEqual(normalize_time_of_day(raw), "day", raw)
        for raw in ("夕方", "夕暮れ", "sunset", "evening", "twilight"):
            self.assertEqual(normalize_time_of_day(raw), "dusk", raw)
        for raw in ("夜", "night", "midnight", "夜景"):
            self.assertEqual(normalize_time_of_day(raw), "night", raw)

    def test_case_and_separator_variants(self) -> None:
        for raw in ("Night", "NIGHT", "night_time", "night-time", " night "):
            self.assertEqual(normalize_time_of_day(raw), "night", raw)

    def test_compound_expressions_are_matched(self) -> None:
        self.assertEqual(normalize_time_of_day("early morning"), "day")
        self.assertEqual(normalize_time_of_day("夜の街並み"), "night")

    def test_unknown_values_become_unspecified(self) -> None:
        for raw in ("", None, "雨", "abstract", "unknown"):
            self.assertEqual(normalize_time_of_day(raw), "unspecified", str(raw))


class AssetTimeOfDayTest(unittest.TestCase):
    def test_classifies_night_dusk_and_day(self) -> None:
        self.assertEqual(_asset_time_of_day("皿倉山夜景03", []), "night")
        self.assertEqual(
            _asset_time_of_day("若戸大橋ライトアップ08", []), "night"
        )
        self.assertEqual(_asset_time_of_day("門司港の夕景", []), "dusk")
        self.assertEqual(_asset_time_of_day("小倉城01", []), "unknown_or_day")
        self.assertEqual(
            _asset_time_of_day("風景", ["イルミネーション・夜景"]), "night"
        )


class TodEvalTest(unittest.TestCase):
    def test_day_cut_excludes_night_asset(self) -> None:
        excluded, _ = _tod_eval("day", "night")
        self.assertTrue(excluded)

    def test_night_cut_prefers_night_asset(self) -> None:
        _, night_score = _tod_eval("night", "night")
        _, day_score = _tod_eval("night", "unknown_or_day")
        self.assertGreater(night_score, day_score)

    def test_dusk_cut_prefers_exact_dusk(self) -> None:
        _, dusk_score = _tod_eval("dusk", "dusk")
        _, night_score = _tod_eval("dusk", "night")
        self.assertGreater(dusk_score, night_score)


class ShortlistTimeOfDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            _candidate("asset-001", "皿倉山夜景03"),
            _candidate("asset-002", "若戸大橋ライトアップ08"),
            _candidate("asset-003", "小倉城01"),
            _candidate("asset-004", "門司港駅09"),
            _candidate("asset-005", "門司港の夕景"),
        ]

    def test_morning_cut_never_receives_night_assets(self) -> None:
        """LLMが「朝」と書いても、正規化後に夜景が選ばれてはならない。"""
        cut = {
            "id": 1,
            "time_of_day": normalize_time_of_day("朝"),
            "location": "小倉",
            "visual_role": "opening",
            "name": "朝の始まり",
            "subject": "街",
        }
        shortlisted = _shortlist_candidates([cut], self.candidates, "夜景賞")
        picked = [c for c in shortlisted if 1 in c["eligible_cut_ids"]]
        self.assertTrue(picked)
        self.assertFalse(
            [c for c in picked if c["time_of_day"] == "night"],
            "朝(=day)のカットに夜景素材が選ばれている",
        )

    def test_night_cut_prefers_night_assets(self) -> None:
        cut = {
            "id": 2,
            "time_of_day": "night",
            "location": "皿倉",
            "visual_role": "climax",
            "name": "夜景",
            "subject": "夜景",
        }
        shortlisted = _shortlist_candidates([cut], self.candidates, "夜景賞")
        ranked = sorted(
            (c for c in shortlisted if 2 in c["eligible_cut_ids"]),
            key=lambda c: -c["scores_by_cut"]["2"],
        )
        self.assertEqual(ranked[0]["time_of_day"], "night")

    def test_relaxes_instead_of_returning_zero_candidates(self) -> None:
        """夜景素材しかなくても、昼カットで候補ゼロにせず緩和する。"""
        night_only = [
            _candidate("asset-001", "皿倉山夜景03"),
            _candidate("asset-002", "若戸大橋ライトアップ08"),
        ]
        cut = {
            "id": 3,
            "time_of_day": "day",
            "location": "小倉",
            "visual_role": "opening",
            "name": "昼",
            "subject": "街",
        }
        shortlisted = _shortlist_candidates([cut], night_only, "夜景賞")
        self.assertTrue(
            [c for c in shortlisted if 3 in c["eligible_cut_ids"]],
            "候補ゼロで停止せず緩和されるべき",
        )
        self.assertIn(
            3, getattr(_shortlist_candidates, "last_relaxed_cut_ids", [])
        )


if __name__ == "__main__":
    unittest.main()
