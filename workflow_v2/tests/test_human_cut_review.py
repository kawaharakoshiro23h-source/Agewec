"""人間によるカット差し戻し・seed変更・レビュー画面の回帰テスト。"""
from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from agewec_v2.pipeline_runtime import (
    _stable_seed,
    commit_cut_qa,
    cut_visual_qa,
    image_video_production,
)
from agewec_v2.review_page import build_review_page


def _state(**overrides):
    base = {
        "run_id": "run-test",
        "current_cut_id": 3,
        "production_queue": [3, 4],
        "approved_cut_ids": [1, 2],
        "failed_cut_ids": [],
        "cut_qa_results": {
            "3": {
                "cut_id": 3,
                "verdict": "pass",          # AIは合格と判定している
                "recommended_route": "next_cut",
                "issue_class": "pass",
            }
        },
        "production_artifacts": {"3": {"path": "/tmp/cut3.mp4"}},
        "review_context": {},
        "feedback": {},
        "events": [],
    }
    base.update(overrides)
    return base


class HumanCutDecisionTest(unittest.TestCase):
    def test_ai_pass_is_followed_when_no_human_decision(self) -> None:
        """後方互換: 人間の判断が無ければ従来どおりAI判定に従う。"""
        update = commit_cut_qa(_state())
        self.assertEqual(update["cut_qa_route"], "next_cut")
        self.assertIn(3, update["approved_cut_ids"])

    def test_human_overrides_ai_pass_and_routes_to_director(self) -> None:
        """人間が『演出を直す』と判断したら、AIのpassより優先される。"""
        state = _state(
            human_cut_qa_decisions={
                "3": {
                    "verdict": "revise",
                    "route": "director",
                    "feedback": "知らない人物が写り込んでいる",
                    "issue_class": "direction",
                }
            }
        )
        update = commit_cut_qa(state)
        self.assertEqual(update["cut_qa_route"], "director")
        self.assertNotIn(3, update["approved_cut_ids"])
        self.assertIn(3, update["failed_cut_ids"])
        self.assertEqual(
            update["review_context"]["director"]["target_cut_id"], 3
        )
        self.assertIn("知らない人物", update["feedback"]["director"])

    def test_each_route_is_dispatched(self) -> None:
        for route in (
            "director",
            "asset_curator",
            "support_video_creator",
            "image_video_production",
        ):
            with self.subTest(route=route):
                state = _state(
                    human_cut_qa_decisions={
                        "3": {"verdict": "revise", "route": route}
                    }
                )
                update = commit_cut_qa(state)
                self.assertEqual(update["cut_qa_route"], route)

    def test_approved_cuts_are_not_broken_by_revision(self) -> None:
        """差し戻しても、既に承認済みのカットは失われない。"""
        state = _state(
            human_cut_qa_decisions={
                "3": {"verdict": "revise", "route": "director"}
            }
        )
        update = commit_cut_qa(state)
        self.assertIn(1, update["approved_cut_ids"])
        self.assertIn(2, update["approved_cut_ids"])

    def test_decision_does_not_leak_to_other_cuts(self) -> None:
        """cut 3への指示が cut 4 に適用されてはならない。"""
        state = _state(
            human_cut_qa_decisions={
                "3": {"verdict": "revise", "route": "director"}
            }
        )
        first = commit_cut_qa(state)
        # cut 3 の判断は使用後に破棄される
        self.assertNotIn("3", first["human_cut_qa_decisions"])

        # 次に cut 4 を評価する状況を作る（AIはpass判定）
        second_state = _state(
            current_cut_id=4,
            production_queue=[4],
            cut_qa_results={
                "4": {
                    "cut_id": 4,
                    "verdict": "pass",
                    "recommended_route": "next_cut",
                }
            },
            production_artifacts={"4": {"path": "/tmp/cut4.mp4"}},
            human_cut_qa_decisions=first["human_cut_qa_decisions"],
        )
        second = commit_cut_qa(second_state)
        self.assertIn(4, second["approved_cut_ids"])
        self.assertNotEqual(second["cut_qa_route"], "director")

    def test_human_decision_is_logged_as_event(self) -> None:
        state = _state(
            human_cut_qa_decisions={
                "3": {"verdict": "revise", "route": "asset_curator"}
            }
        )
        update = commit_cut_qa(state)
        kinds = [e.get("type") for e in update["events"]]
        self.assertIn("human_cut_decision_applied", kinds)
        committed = [
            e for e in update["events"] if e.get("type") == "cut_qa_committed"
        ][0]
        self.assertEqual(committed["decided_by"], "human")


class SeedRegenerationIntegrationTest(unittest.TestCase):
    """「同じ条件で再生成」が実際に別のseedで生成されることを検証する。

    seed は support_video_creator で作られるが、再生成ルートはそこを通らず
    image_video_production へ直接戻る。生成直前に引き直していないと、
    再生成しても同じ映像になる（この経路の回帰テスト）。
    """

    def _state(
        self,
        attempts: dict[str, int],
        *,
        work_dir: str = "work",
    ):
        return {
            "run_id": "run-seed",
            "current_cut_id": 1,
            "cut_attempts": dict(attempts),
            "production_requests": {
                "1": {
                    "cut_id": 1,
                    "backend": "mock",
                    "image_path": "/tmp/x.jpg",
                    "positive_prompt": "p",
                    "negative_prompt": "n",
                    "width": 64,
                    "height": 64,
                    "frames": 9,
                    "steps": 1,
                    "fps": 8,
                    "seed": 111111,          # 古いseedが残っている状態
                    "actual_seconds": 1.125,
                    "media_requirement": "video_required",
                }
            },
            "production_artifacts": {},
            "cut_results": {},
            "config": {
                "execution_limits": {},
                "paths": {"work_dir": work_dir},
                "qa": {"representative_frame_count": 1},
            },
            "phase_results": {},
            "attempts": {},
            "events": [],
            "artifacts": [],
        }

    def test_regeneration_uses_a_new_seed(self) -> None:
        first = image_video_production(self._state({}))
        seed_1 = first["production_requests"]["1"]["seed"]

        # 1回目の attempt を引き継いで再生成
        second = image_video_production(
            self._state(first["cut_attempts"])
        )
        seed_2 = second["production_requests"]["1"]["seed"]

        self.assertNotEqual(
            seed_1, seed_2, "再生成しても seed が変わっていない"
        )
        self.assertNotEqual(seed_1, 111111, "古いseedが再利用されている")
        self.assertEqual(second["cut_attempts"]["1"], 2)

    def test_first_attempt_is_reproducible(self) -> None:
        a = image_video_production(self._state({}))
        b = image_video_production(self._state({}))
        self.assertEqual(
            a["production_requests"]["1"]["seed"],
            b["production_requests"]["1"]["seed"],
        )

    def test_attempt_metadata_is_preserved_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = self._state({}, work_dir=tmp)
            first = image_video_production(base)
            first_state = {**base, **first}
            first_qa = cut_visual_qa(first_state)
            first_complete = {**first_state, **first_qa}
            commit_cut_qa(first_complete)

            second_base = self._state(
                first["cut_attempts"],
                work_dir=tmp,
            )
            second = image_video_production(second_base)
            second_state = {**second_base, **second}
            second_qa = cut_visual_qa(second_state)
            second_complete = {**second_state, **second_qa}
            commit_cut_qa(second_complete)

            cut_dir = Path(tmp) / "runs" / "run-seed" / "cuts" / "cut_01"
            expected = (
                "attempt_01_request.json",
                "attempt_01_qa.json",
                "attempt_01_decision.json",
                "attempt_02_request.json",
                "attempt_02_qa.json",
                "attempt_02_decision.json",
            )
            for name in expected:
                self.assertTrue((cut_dir / name).exists(), name)

            first_request = json.loads(
                (cut_dir / "attempt_01_request.json").read_text(
                    encoding="utf-8"
                )
            )
            second_request = json.loads(
                (cut_dir / "attempt_02_request.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotEqual(first_request["seed"], second_request["seed"])
            self.assertEqual(first_request["attempt"], 1)
            self.assertEqual(second_request["attempt"], 2)


class SeedVariationTest(unittest.TestCase):
    def test_same_attempt_is_reproducible(self) -> None:
        self.assertEqual(
            _stable_seed("run-a", 1, 0), _stable_seed("run-a", 1, 0)
        )

    def test_retry_changes_seed(self) -> None:
        """再生成（attempt増加）で別のseed＝別の結果になる。"""
        self.assertNotEqual(
            _stable_seed("run-a", 1, 0), _stable_seed("run-a", 1, 1)
        )

    def test_cuts_have_distinct_seeds(self) -> None:
        self.assertNotEqual(
            _stable_seed("run-a", 1, 0), _stable_seed("run-a", 2, 0)
        )


class ReviewPageTest(unittest.TestCase):
    def test_builds_page_with_cut_sections(self) -> None:
        state = {
            "run_id": "run-test",
            "current_cut_id": 1,
            "approved_cut_ids": [],
            "phase_results": {
                "writer_storyboard": {
                    "data": {
                        "cuts": [
                            {
                                "id": 1,
                                "name": "夜景",
                                "scene": "皿倉山からの夜景",
                                "time_of_day": "night",
                                "location": "皿倉",
                                "visual_role": "climax",
                                "seconds": 5,
                            }
                        ]
                    }
                },
                "director": {
                    "data": {
                        "shots": [
                            {
                                "id": 1,
                                "positive_prompt": "cinematic night view",
                                "negative_prompt": "distorted",
                                "camera_motion": "slow push-in",
                                "asset": {
                                    "asset_id": "asset-001",
                                    "title": "皿倉山夜景03",
                                    "selection_reason": "夜景に最適",
                                },
                            }
                        ]
                    }
                },
            },
            "production_requests": {"1": {"seed": 12345}},
            "production_artifacts": {},
            "cut_qa_results": {"1": {"verdict": "pass", "issues": []}},
            "artifacts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = build_review_page(state, Path(tmp) / "review.html")
            html = out.read_text(encoding="utf-8")
        self.assertIn("Cut 1", html)
        self.assertIn("皿倉山夜景03", html)
        self.assertIn("夜景に最適", html)
        self.assertIn("cinematic night view", html)
        self.assertIn("12345", html)

    def test_escapes_untrusted_text(self) -> None:
        state = {
            "run_id": "r",
            "phase_results": {
                "writer_storyboard": {
                    "data": {"cuts": [{"id": 1, "name": "<script>x</script>"}]}
                }
            },
            "artifacts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = build_review_page(state, Path(tmp) / "review.html")
            html = out.read_text(encoding="utf-8")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
