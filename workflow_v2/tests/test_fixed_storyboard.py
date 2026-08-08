"""configに書いた絵コンテをそのまま使う経路の検証。

使う写真が先に決まっている制作では、絵コンテをLLMに書かせると毎回
抽象的な内容（「夜の街並みを俯瞰する」など）が出て、毎回人間が差し戻す。
実際に run-d35ee139e1 では Director を19回実行してもプロンプトが
写真に合わず、最終的に絵コンテ由来の汎用文へ戻った。
"""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from typing import Any

import yaml

from agewec_v2 import nodes_llm


ROOT = Path(__file__).resolve().parents[1]


def _cut(cut_id: int, seconds: float = 5.0, **extra: Any) -> dict[str, Any]:
    return {
        "id": cut_id,
        "name": f"カット{cut_id}",
        "scene": f"シーン{cut_id}",
        "seconds": seconds,
        **extra,
    }


class FixedStoryboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config: dict[str, Any] = {
            "project": {"target_duration_seconds": 10},
            "storyboard": {},
            "production": {},
            "llm": {"enabled": False},
        }

    def _state(self) -> dict[str, Any]:
        return {
            "run_id": "fixed-storyboard-test",
            "project": self.config["project"],
            "config": self.config,
            "phase_results": {
                "executive_producer": {
                    "phase": "executive_producer", "status": "success",
                    "data": {"target_duration_seconds":
                             self.config["project"]["target_duration_seconds"]},
                },
                "creative_director": {
                    "phase": "creative_director", "status": "success",
                    "data": {"title": "テスト"},
                },
            },
            "attempts": {}, "feedback": {}, "review_context": {},
            "reviews": [], "events": [], "artifacts": [], "aborted": False,
        }

    def _run(self) -> dict[str, Any]:
        out = nodes_llm.writer_storyboard(self._state())
        return out["phase_results"]["writer_storyboard"]

    # --- 本題 -----------------------------------------------------------

    def test_uses_the_configured_cuts_verbatim(self) -> None:
        self.config["storyboard"]["fixed_cuts"] = [
            _cut(1, 4.0, scene="薄暮の門司港駅"),
            _cut(2, 6.0, scene="夜の赤煉瓦プレイス"),
        ]
        result = self._run()
        self.assertEqual(result["status"], "success")
        cuts = result["data"]["cuts"]
        self.assertEqual([c["scene"] for c in cuts],
                         ["薄暮の門司港駅", "夜の赤煉瓦プレイス"])
        self.assertEqual([c["seconds"] for c in cuts], [4.0, 6.0])
        self.assertEqual(result["data"]["total_seconds"], 10.0)
        self.assertEqual(result["data"]["source"], "config.storyboard.fixed_cuts")

    def test_does_not_call_the_llm(self) -> None:
        """LLMを呼ばないこと。呼ぶと内容が書き換わり、固定した意味が消える。"""
        self.config["storyboard"]["fixed_cuts"] = [_cut(1, 10.0)]
        called = []

        original = nodes_llm._run_role
        nodes_llm._run_role = lambda *a, **k: called.append(1)
        try:
            result = self._run()
        finally:
            nodes_llm._run_role = original
        self.assertEqual(called, [], "LLMが呼ばれた")
        self.assertEqual(result["status"], "success")

    def test_does_not_rescale_the_durations(self) -> None:
        """人間が書いた秒数を勝手に伸縮しないこと。

        再スケールされると、Runwayの整数秒に合わせた設計が崩れる。
        """
        self.config["project"]["target_duration_seconds"] = 30
        self.config["storyboard"]["fixed_cuts"] = [_cut(1, 5.0), _cut(2, 5.0)]
        result = self._run()
        self.assertEqual([c["seconds"] for c in result["data"]["cuts"]], [5.0, 5.0])
        self.assertEqual(result["data"]["total_seconds"], 10.0)

    def test_warns_when_the_total_differs_from_the_target(self) -> None:
        """黙って合わせず、人間に知らせる。"""
        self.config["project"]["target_duration_seconds"] = 30
        self.config["storyboard"]["fixed_cuts"] = [_cut(1, 5.0)]
        result = self._run()
        self.assertTrue(
            any("一致しません" in str(w) for w in result.get("warnings", [])),
            result.get("warnings"),
        )

    def test_does_not_thin_out_cuts(self) -> None:
        """max_video_cuts_per_run で間引かないこと。

        人間が8カットと決めたなら8カット使う。間引くと指定した写真が
        余り、素材の割り当てとずれる。
        """
        self.config["production"]["max_video_cuts_per_run"] = 2
        self.config["storyboard"]["fixed_cuts"] = [_cut(i) for i in (1, 2, 3, 4)]
        result = self._run()
        self.assertEqual(len(result["data"]["cuts"]), 4)
        self.assertTrue(
            any("間引かず" in str(w) for w in result.get("warnings", [])),
            result.get("warnings"),
        )

    def test_fills_in_optional_fields(self) -> None:
        """scene・name・seconds だけ書けば動くこと。"""
        self.config["storyboard"]["fixed_cuts"] = [
            {"name": "テスト", "scene": "夜景", "seconds": 10}
        ]
        cut = self._run()["data"]["cuts"][0]
        self.assertEqual(cut["id"], 1)
        self.assertEqual(cut["media_requirement"], "video_required")
        self.assertIn("time_of_day", cut)
        self.assertIn("visual_role", cut)

    def test_sorts_by_id(self) -> None:
        self.config["storyboard"]["fixed_cuts"] = [_cut(3), _cut(1), _cut(2)]
        ids = [c["id"] for c in self._run()["data"]["cuts"]]
        self.assertEqual(ids, [1, 2, 3])

    # --- 誤りを弾く -----------------------------------------------------

    def test_rejects_duplicate_ids(self) -> None:
        self.config["storyboard"]["fixed_cuts"] = [_cut(1), _cut(1)]
        with self.assertRaises(ValueError):
            self._run()

    def test_rejects_a_non_positive_duration(self) -> None:
        self.config["storyboard"]["fixed_cuts"] = [_cut(1, 0.0)]
        with self.assertRaises(ValueError):
            self._run()

    # --- 既定を壊さない --------------------------------------------------

    def test_absent_or_empty_falls_back_to_the_llm(self) -> None:
        """未設定・空なら従来どおりLLMに書かせること。"""
        for value in (None, []):
            with self.subTest(value=value):
                self.config["storyboard"].pop("fixed_cuts", None)
                if value is not None:
                    self.config["storyboard"]["fixed_cuts"] = value
                called = []
                original = nodes_llm._run_role
                nodes_llm._run_role = lambda *a, **k: (called.append(1), {})[1]
                try:
                    nodes_llm.writer_storyboard(self._state())
                finally:
                    nodes_llm._run_role = original
                self.assertEqual(len(called), 1, "LLM経路に入らなかった")


class ShippedConfigTest(unittest.TestCase):
    def test_the_shipped_config_produces_the_intended_storyboard(self) -> None:
        """同梱のconfigが、意図した8カット45秒になっていること。"""
        config = yaml.safe_load(
            (ROOT / "config_llm.yaml").read_text(encoding="utf-8")
        )
        cuts = config.get("storyboard", {}).get("fixed_cuts")
        if not cuts:
            self.skipTest("fixed_cuts が設定されていない")
        self.assertEqual(len(cuts), 8)
        total = sum(float(c["seconds"]) for c in cuts)
        self.assertEqual(
            float(config["project"]["target_duration_seconds"]), total,
            "target_duration_seconds と固定絵コンテの合計がずれている",
        )
        # Runwayは整数秒しか受け付けない。小数を書くと切り上げ課金になる。
        for cut in cuts:
            self.assertEqual(
                float(cut["seconds"]), int(cut["seconds"]),
                f"Cut {cut['id']} の seconds が整数ではない",
            )
        self.assertEqual(
            int(config["production"]["max_video_cuts_per_run"]), 8
        )


if __name__ == "__main__":
    unittest.main()
