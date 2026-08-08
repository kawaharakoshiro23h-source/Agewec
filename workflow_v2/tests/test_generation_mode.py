"""カット単位の generation_mode（image_to_video / text_to_video）の検証。

設計上の約束:
  1. 既定は image_to_video。既存runと同じ振る舞いを保つ
  2. 自動フォールバックはしない。画像が無いことを理由に t2v へ落とさない
  3. 方式と入力の不一致は、課金の前に止める
  4. t2v カットは「素材が抜けている」ではなく「完全生成」と記録・表示する
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pydantic
from lxml import html as LH

from agewec_v2.backends import to_video_request
from agewec_v2 import nodes_llm
from agewec_v2.llm.schemas import DirectionPlan, DirectionShot
from agewec_v2.pipeline_runtime import (
    _copy_cut_sources,
    _phase_visual_cards,
    _request_summary,
    support_video_creator,
)
from agewec_v2.review_display import _cut_visual_qa, _support_video


def _shot(cut_id: int, mode: str, *, asset_id: str | None) -> dict:
    return {
        "cut_id": cut_id,
        "generation_mode": mode,
        "asset_id": asset_id,
        "positive_prompt": f"prompt {cut_id}",
        "negative_prompt": "",
        "camera_motion": "slow push-in",
        "motion_intensity": "subtle",
        "rationale": "理由",
        "camera_intent_alignment": "整合",
    }


class SchemaTest(unittest.TestCase):
    def test_default_is_image_to_video(self) -> None:
        """既存の絵コンテ（modeなし）は従来どおり動く。"""
        shot = DirectionShot(**{
            k: v for k, v in _shot(1, "image_to_video", asset_id="asset-001").items()
            if k != "generation_mode"
        })
        self.assertEqual(shot.generation_mode, "image_to_video")
        self.assertIsNone(shot.model)

    def test_model_is_optional_but_cannot_be_blank(self) -> None:
        shot = DirectionShot(**{
            **_shot(1, "image_to_video", asset_id="asset-001"),
            "model": "gen4.5",
        })
        self.assertEqual(shot.model, "gen4.5")
        with self.assertRaises(pydantic.ValidationError):
            DirectionShot(**{
                **_shot(1, "image_to_video", asset_id="asset-001"),
                "model": "  ",
            })

    def test_static_is_a_valid_motion_intensity(self) -> None:
        """「カメラは固定」を表現できること。

        static が無かったため、人間が「固定で」と指示したカットで
        Directorが static を返し、検証に3回連続で失敗して実行が停止した
        （run-fc15a3798d）。静止画からの生成では最も多用する値。
        """
        for value in ("static", "subtle", "moderate", "strong"):
            with self.subTest(value=value):
                shot = DirectionShot(**{
                    **_shot(1, "image_to_video", asset_id="asset-001"),
                    "motion_intensity": value,
                })
                self.assertEqual(shot.motion_intensity, value)

    def test_unknown_motion_intensity_is_still_rejected(self) -> None:
        for value in ("medium", "none", "強い", ""):
            with self.subTest(value=value):
                with self.assertRaises(pydantic.ValidationError):
                    DirectionShot(**{
                        **_shot(1, "image_to_video", asset_id="asset-001"),
                        "motion_intensity": value,
                    })

    def test_the_prompt_lists_exactly_the_allowed_values(self) -> None:
        """MDとスキーマがずれていないこと。

        ずれると、Directorが存在しない値を返して実行が止まる。
        """
        import typing
        from pathlib import Path
        allowed = set(
            typing.get_args(
                DirectionShot.model_fields["motion_intensity"].annotation
            )
        )
        md = (
            Path(__file__).resolve().parents[1]
            / "agewec_v2" / "prompts" / "director.md"
        ).read_text(encoding="utf-8")
        for value in allowed:
            self.assertIn(f"`{value}`", md, f"{value} がdirector.mdに無い")

    def test_image_to_video_requires_asset(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            DirectionShot(**_shot(1, "image_to_video", asset_id=None))

    def test_text_to_video_rejects_asset(self) -> None:
        """素材を指定したまま t2v にするのは指示の矛盾なので弾く。"""
        with self.assertRaises(pydantic.ValidationError):
            DirectionShot(**_shot(1, "text_to_video", asset_id="asset-001"))

    def test_text_to_video_without_asset_is_valid(self) -> None:
        shot = DirectionShot(**_shot(1, "text_to_video", asset_id=None))
        self.assertIsNone(shot.asset_id)

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            DirectionShot(**_shot(1, "video_to_video", asset_id=None))

    def test_plan_accepts_mixed_modes(self) -> None:
        plan = DirectionPlan(
            shots=[
                DirectionShot(**_shot(1, "image_to_video", asset_id="asset-001")),
                DirectionShot(**_shot(2, "text_to_video", asset_id=None)),
            ],
            continuity_checks=["色調の連続性"],
        )
        self.assertEqual(
            [s.generation_mode for s in plan.shots],
            ["image_to_video", "text_to_video"],
        )


class DirectorModelSelectionTest(unittest.TestCase):
    def _state(self) -> dict:
        return {
            "config": {
                "production": {
                    "backend": "runway",
                    "model": "gen4.5",
                },
                "runway": {
                    "models": {
                        "gen4.5": {
                            "allowed_seconds": list(range(2, 11)),
                            "resolutions": ["1280:720"],
                            "cost_per_second_usd": 0.12,
                        },
                        "hailuo3": {
                            "allowed_seconds": list(range(5, 16)),
                            "resolutions": ["768P"],
                            "generation_modes": [
                                "image_to_video", "text_to_video"
                            ],
                            "has_native_audio": True,
                            "cost_per_second_usd": 0.15,
                        },
                    }
                },
            },
            "phase_results": {
                "writer_storyboard": {
                    "data": {"cuts": [{
                        "id": 1, "name": "夜景", "seconds": 5.0,
                    }]},
                },
                "asset_curator": {
                    "data": {"asset_assignments": [{
                        "cut_id": 1,
                        "primary": {"asset_id": "asset-001"},
                        "alternatives": [],
                    }]},
                },
                "creative_director": {"data": {"camera_intent": {}}},
            },
            "review_context": {},
            "feedback": {},
        }

    @staticmethod
    def _llm_output(model: str | None) -> dict:
        return {
            "shots": [{
                "cut_id": 1,
                "generation_mode": "image_to_video",
                "model": model,
                "asset_id": "asset-001",
                "positive_prompt": "night view",
                "negative_prompt": "",
                "camera_motion": "slow pan",
                "motion_intensity": "subtle",
                "rationale": "モデル適性に基づく",
                "camera_intent_alignment": "整合",
                "deviation_reason": None,
            }],
            "continuity_checks": ["色調を維持"],
        }

    def test_director_carries_selected_model_and_receives_capabilities(self) -> None:
        captured = {}

        def fake_run_role(_state, **kwargs):
            captured.update(kwargs["upstream"]["video_model_policy"])
            return kwargs["transform"](self._llm_output("hailuo3"))

        with patch.object(nodes_llm, "_run_role", side_effect=fake_run_role):
            result = nodes_llm.director(self._state())

        self.assertEqual(result["shots"][0]["model"], "hailuo3")
        self.assertEqual(captured["default_model"], "gen4.5")
        self.assertEqual(
            [item["model"] for item in captured["available_models"]],
            ["gen4.5", "hailuo3"],
        )

    def test_director_uses_default_when_model_is_omitted(self) -> None:
        def fake_run_role(_state, **kwargs):
            return kwargs["transform"](self._llm_output(None))

        with patch.object(nodes_llm, "_run_role", side_effect=fake_run_role):
            result = nodes_llm.director(self._state())

        self.assertEqual(result["shots"][0]["model"], "gen4.5")


def _state(tmp: str, shots: list[dict]) -> dict:
    return {
        "run_id": "run-mode-test",
        "config": {
            "paths": {"work_dir": tmp},
            "production": {
                "backend": "runway",
                "model": "gen4.5",
                "profile": "draft",
                "profiles": {"draft": {"width": 576, "height": 384,
                                       "fps": 24, "steps": 20}},
                "cost_guard": {"enabled": False},
            },
            "runway": {
                "ratio": "1280:720",
                "models": {"gen4.5": {
                    "allowed_seconds": list(range(2, 11)),
                    "resolutions": ["1280:720"],
                    "cost_per_second_usd": 0.12,
                }, "hailuo3": {
                    "allowed_seconds": list(range(5, 16)),
                    "resolutions": ["768P"],
                    "resolution": "768P",
                    "generation_modes": ["image_to_video", "text_to_video"],
                    "supports_seed": False,
                    "supports_negative_prompt": False,
                    "cost_per_second_usd": 0.15,
                }},
            },
        },
        "phase_results": {"director": {"data": {"shots": shots}}},
        "production_requests": {},
        "review_context": {},
        "attempts": {},
        "cut_attempts": {},
        "events": [],
        "artifacts": [],
    }


def _director_shot(
    cut_id: int,
    mode: str,
    image: str | None,
    *,
    model: str | None = None,
) -> dict:
    return {
        "id": cut_id,
        "name": f"カット{cut_id}",
        "seconds": 5.0,
        "time_of_day": "night",
        "generation_mode": mode,
        **({"model": model} if model else {}),
        "asset": ({"asset_id": f"asset-{cut_id:03d}", "title": f"素材{cut_id}",
                   "local_path": image} if image else None),
        "positive_prompt": f"prompt {cut_id}",
        "negative_prompt": "",
        "camera_motion": "slow push-in",
        "motion_intensity": "subtle",
        "rationale": "理由",
    }


class Phase055ValidationTest(unittest.TestCase):
    """方式と入力の不一致は、課金を伴う生成の前に止める。"""

    def _run(self, shots: list[dict]):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"RUNWAY_API_KEY": "test-key"}):
                return support_video_creator(_state(tmp, shots))

    def test_text_to_video_needs_no_image(self) -> None:
        result = self._run([_director_shot(1, "text_to_video", None)])
        self.assertEqual(
            result["phase_results"]["support_video_creator"]["status"],
            "success",
        )
        request = result["production_requests"]["1"]
        self.assertEqual(request["generation_mode"], "text_to_video")
        self.assertEqual(request["image_path"], "")

    def test_image_to_video_still_requires_an_existing_image(self) -> None:
        """既存の保護を弱めていないこと。"""
        result = self._run(
            [_director_shot(1, "image_to_video", "/tmp/does-not-exist.jpg")]
        )
        blocking = result["phase_results"]["support_video_creator"][
            "blocking_issues"
        ]
        self.assertTrue(
            any("ローカル入力画像が存在しない" in item for item in blocking),
            blocking,
        )

    def test_text_to_video_with_an_image_is_blocked(self) -> None:
        """画像があるのに t2v は指示の矛盾。黙って無視せず止める。"""
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image:
            result = self._run(
                [_director_shot(1, "text_to_video", image.name)]
            )
        blocking = result["phase_results"]["support_video_creator"][
            "blocking_issues"
        ]
        self.assertTrue(
            any("text_to_video に画像は指定できません" in i for i in blocking),
            blocking,
        )

    def test_unknown_mode_is_blocked(self) -> None:
        result = self._run([_director_shot(1, "video_to_video", None)])
        blocking = result["phase_results"]["support_video_creator"][
            "blocking_issues"
        ]
        self.assertTrue(
            any("未知の generation_mode" in item for item in blocking),
            blocking,
        )

    def test_mixed_modes_produce_one_request_each(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image:
            result = self._run([
                _director_shot(1, "image_to_video", image.name),
                _director_shot(2, "text_to_video", None),
            ])
        requests = result["production_requests"]
        self.assertEqual(requests["1"]["generation_mode"], "image_to_video")
        self.assertEqual(requests["1"]["image_path"], image.name)
        self.assertEqual(requests["2"]["generation_mode"], "text_to_video")
        self.assertEqual(requests["2"]["image_path"], "")

    def test_mixed_models_use_each_cut_contract_and_cost(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image:
            result = self._run([
                _director_shot(
                    1, "image_to_video", image.name, model="gen4.5"
                ),
                _director_shot(
                    2, "text_to_video", None, model="hailuo3"
                ),
            ])
        requests = result["production_requests"]
        self.assertEqual(requests["1"]["model"], "gen4.5")
        self.assertEqual(requests["1"]["ratio"], "1280:720")
        self.assertEqual(requests["2"]["model"], "hailuo3")
        self.assertEqual(requests["2"]["resolution"], "768P")
        estimate = result["phase_results"]["support_video_creator"]["data"][
            "cost_estimate"
        ]
        self.assertEqual(estimate["models"], ["gen4.5", "hailuo3"])
        self.assertAlmostEqual(estimate["total_usd"], 1.35)
        self.assertEqual(
            [cut["model"] for cut in estimate["cuts"]],
            ["gen4.5", "hailuo3"],
        )

    def test_missing_cut_model_uses_production_default(self) -> None:
        result = self._run([_director_shot(1, "text_to_video", None)])
        self.assertEqual(result["production_requests"]["1"]["model"], "gen4.5")

    def test_unknown_cut_model_is_blocked_before_generation(self) -> None:
        result = self._run([
            _director_shot(1, "text_to_video", None, model="unknown-model")
        ])
        blocking = result["phase_results"]["support_video_creator"][
            "blocking_issues"
        ]
        self.assertTrue(any("unknown-model" in item for item in blocking), blocking)


class VideoRequestTest(unittest.TestCase):
    def test_mode_is_carried_to_the_backend(self) -> None:
        request = to_video_request(
            {"cut_id": 1, "generation_mode": "text_to_video"}, attempt=1
        )
        self.assertEqual(request.extra["generation_mode"], "text_to_video")

    def test_missing_mode_defaults_to_image_to_video(self) -> None:
        """過去runのProductionRequestを読み直しても壊れないこと。"""
        request = to_video_request({"cut_id": 1}, attempt=1)
        self.assertEqual(request.extra["generation_mode"], "image_to_video")


class PresentationTest(unittest.TestCase):
    """t2v は「素材の欠落」ではなく「完全生成」として見せる。"""

    def test_cli_shows_the_mode_before_approval(self) -> None:
        lines = _support_video({
            "backend": "runway", "model": "gen4.5",
            "requests": [
                {"cut_id": 1, "generation_mode": "text_to_video",
                 "model": "gen4.5",
                 "requested_seconds": 5.0, "image_path": "",
                 "camera_motion": "pan", "positive_prompt": "p"},
            ],
        })
        joined = "\n".join(lines)
        self.assertIn("Text to Video", joined)
        self.assertIn("使用モデル: gen4.5", joined)
        self.assertNotIn("元画像: —", joined)

    def test_cut_qa_shows_the_mode(self) -> None:
        joined = "\n".join(_cut_visual_qa({
            "cut_id": 1, "attempt": 1, "verdict": "pass",
            "generation_mode": "text_to_video",
            "artifact_path": "/tmp/cut.mp4", "source_image": "",
            "issues": [],
        }))
        self.assertIn("Text to Video", joined)

    def test_report_card_labels_generated_footage(self) -> None:
        markup = _phase_visual_cards("director", _state("/tmp", [
            _director_shot(1, "text_to_video", None),
        ]))
        text = LH.fragment_fromstring(markup).text_content()
        self.assertIn("完全生成映像", text)
        self.assertIn("Text to Video", text)

    def test_report_card_keeps_asset_line_for_image_to_video(self) -> None:
        markup = _phase_visual_cards("director", _state("/tmp", [
            _director_shot(1, "image_to_video", "/tmp/a.jpg"),
        ]))
        text = LH.fragment_fromstring(markup).text_content()
        self.assertIn("使用素材", text)
        self.assertNotIn("完全生成映像", text)

    def test_request_summary_marks_t2v(self) -> None:
        self.assertIn("t2v", _request_summary({
            "generation_mode": "text_to_video",
            "width": 1280, "height": 720, "model": "gen4.5",
            "request_contract": "runway_model_native",
        }))
        self.assertNotIn("t2v", _request_summary({
            "generation_mode": "image_to_video",
            "width": 1280, "height": 720, "model": "gen4.5",
            "request_contract": "runway_model_native",
        }))


class ProvenanceTest(unittest.TestCase):
    def test_cut_sources_records_the_mode(self) -> None:
        """出典が無い理由を証跡から追えるようにする。"""
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            package.mkdir()
            state = _state(tmp, [_director_shot(1, "text_to_video", None)])
            state["production_artifacts"] = {}
            _copy_cut_sources(state, package)
            index = json.loads(
                (package / "cut_sources.json").read_text(encoding="utf-8")
            )
        entry = index["cuts"][0]
        self.assertEqual(entry["generation_mode"], "text_to_video")
        self.assertEqual(entry["model"], "gen4.5")
        self.assertIsNone(entry["asset_id"])
        self.assertIsNone(entry["source_url"])


if __name__ == "__main__":
    unittest.main()
