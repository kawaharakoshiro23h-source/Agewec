from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from agewec_v2.graph import build_graph
from agewec_v2 import nodes_llm
from agewec_v2.roles import assets as asset_role
from agewec_v2.llm.role_runner import (
    ENGLISH_PROMPT_FIELDS,
    MACHINE_TOKEN_FIELDS,
)
from agewec_v2.llm.schemas import ROLE_SCHEMAS
from agewec_v2.nodes_llm import (
    _canonical_asset_id,
    _compact_asset_candidates_for_llm,
    _normalize_japanese_narration,
    _rescale_cut_durations,
)


ROOT = Path(__file__).resolve().parents[1]


ROLE_OUTPUTS = {
    "executive_producer": {
        "objective": "北九州の夜景を紹介する",
        "target_award": "夜景賞",
        "target_duration_seconds": 30,
        "audience": "旅行者",
        "deliverable": "30秒の観光動画",
        "constraints": ["出典を記録する"],
        "success_criteria": ["北九州固有の魅力が伝わる"],
    },
    "creative_director": {
        "title": "光の北九州",
        "logline": "街と産業の光を旅する",
        "tone": ["cinematic"],
        "visual_language": {
            "palette": ["deep blue", "amber"],
            "continuity_rule": "光の方向を維持",
        },
        "camera_intent": {
            "viewer_experience": "昼から荘厳な夜景へ導く",
            "energy_curve": "active_to_calm",
            "stability": "mostly_stable",
            "continuity": "移動方向を自然につなぐ",
            "hard_constraints": ["建築と地形を維持する"],
        },
        "audio_direction": "静かに始まり広がる",
        # 上流基準を転記しない出力でも、コード側で必ず継承される。
        "success_criteria": ["映像全体の色調と動きを統一する"],
    },
    "writer_storyboard": {
        "total_seconds": 30,
        "cuts": [
            {
                "id": 1,
                "name": "導入",
                "scene": "北九州の夜景",
                "narration": (
                    "Welcome to Kitakyushu, where city lights connect "
                    "industry, culture, and everyday life."
                ),
                "seconds": 14,
                "media_requirement": "video_required",
                "time_of_day": "day",
                "visual_role": "opening",
                "location": "小倉",
                "subject": "街と人",
            },
            {
                "id": 2,
                "name": "産業",
                "scene": "工場夜景",
                "narration": "未来を照らす。",
                "seconds": 15,
                "media_requirement": "video_required",
                "time_of_day": "night",
                "visual_role": "climax",
                "location": "皿倉山",
                "subject": "夜景",
            },
        ],
    },
    "visual_qa": {
        "verdict": "pass",
        "route": "post_production",
        "issues": [],
        "cut_results": [
            {"cut_id": 1, "verdict": "pass", "issues": []},
            {"cut_id": 2, "verdict": "pass", "issues": []},
        ],
        "confidence": 0.8,
    },
    "post_production": {
        "operations": [
            {
                "order": 1,
                "operation": "normalize",
                "cut_id": None,
                "parameters": {"fps": 24},
            },
            {
                "order": 2,
                "operation": "concat",
                "cut_id": None,
                "parameters": {},
            },
        ],
        "narration_direction": "簡潔に読む",
        "bgm_direction": "静かな電子音楽",
        "subtitle_direction": "白字で下部",
        "final_duration_seconds": 30,
    },
    "review_board": {
        "rubric_scores": {
            "concept_consistency": 4,
            "story_structure": 4,
            "asset_traceability": 4,
            "technical_completion": 4,
        },
        "average": 4,
        "verdict": "pass",
        "recommendations": [],
        "confidence": 0.8,
    },
}


class FakeChatHandler(BaseHTTPRequestHandler):
    roles: list[str] = []
    payloads: dict[str, dict] = {}

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        user_content = request["messages"][-1]["content"]
        user_payload = json.loads(user_content)
        role = user_payload["role"]
        self.__class__.roles.append(role)
        self.__class__.payloads[role] = user_payload
        upstream = user_payload["approved_upstream_context"]
        if role == "asset_curator_rationale":
            output = {
                "rationales": [
                    {
                        "cut_id": item["cut"]["id"],
                        "reason": (
                            f"{item['selected_asset']['title']}は"
                            "カット条件とコード採点に適合する。"
                        ),
                    }
                    for item in upstream["final_selections"]
                ]
            }
        elif role == "director":
            assignments = upstream["asset_manifest"]["asset_assignments"]
            output = {
                "shots": [
                    {
                        "cut_id": item["cut_id"],
                        "asset_id": item["primary"]["asset_id"],
                        "positive_prompt": (
                            "Kitakyushu cityscape, subtle cinematic motion"
                        ),
                        "negative_prompt": "",
                        "camera_motion": "slow push in",
                        "motion_intensity": "subtle",
                        "rationale": "素材の奥行きを保ちながら見せる",
                        "camera_intent_alignment": "安定した動き",
                        "deviation_reason": None,
                    }
                    for item in assignments
                ],
                "continuity_checks": ["deep blueとamberを維持"],
            }
        else:
            output = copy.deepcopy(ROLE_OUTPUTS[role])
            if role == "executive_producer" and user_payload.get(
                "review_feedback"
            ):
                output["audience"] = user_payload["review_feedback"]
        body = json.dumps(
            {
                "id": f"fake-{role}",
                "model": "fake-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(output, ensure_ascii=False),
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class LLMIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)

    @classmethod
    def setUpClass(cls) -> None:
        FakeChatHandler.roles = []
        FakeChatHandler.payloads = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_all_llm_roles_run_through_common_provider(self) -> None:
        config = yaml.safe_load(
            (ROOT / "config_llm.yaml").read_text(encoding="utf-8")
        )
        # Keep this fixture independent from the user-facing project brief.
        stale_theme = "STALE_CONFIG_THEME_MUST_NOT_REACH_DOWNSTREAM"
        config["project"] = {
            "target_award": "夜景賞",
            "theme": stale_theme,
            "target_duration_seconds": 30,
        }
        config["production"]["backend"] = "mock"
        config["paths"] = {"runtime_dir": self.runtime.name}
        config["production"]["max_video_cuts_per_run"] = 2
        # このテストは「全ロールがLLM経由で動くこと」を見るものなので、
        # 同梱configの固定絵コンテは無効にする（有効だとwriter_storyboardが
        # LLMを呼ばないのが正しい挙動になり、検証の対象がずれる）。
        config.setdefault("storyboard", {})["fixed_cuts"] = []
        config["review_board"]["mode"] = "ai"
        config["final_submission"]["require_human"] = False
        config["autonomy_preset"] = "custom"
        config["review_policies"] = {
            phase: "never"
            for phase in (
                "executive_producer",
                "creative_director",
                "writer_storyboard",
                "asset_curator",
                "director",
                "support_video_creator",
                "image_video_production",
                "cut_visual_qa",
                "visual_qa",
                "post_production",
                "review_board",
                "final_submission",
                "provenance",
            )
        }
        env = {
            "AGEWEC_LLM_ENABLED": "true",
            "AGEWEC_LLM_PROVIDER": "openai_compatible",
            "AGEWEC_LLM_BASE_URL": (
                f"http://127.0.0.1:{self.server.server_port}/v1"
            ),
            "AGEWEC_LLM_API_KEY": "test-secret-never-persist",
            "AGEWEC_LLM_MODEL": "fake-model",
            "AGEWEC_LLM_STRUCTURED_OUTPUT_MODE": "prompt",
            "AGEWEC_LLM_TOKEN_PARAMETER": "max_tokens",
            "AGEWEC_LLM_MAX_RETRIES": "1",
        }
        initial = {
            "run_id": "llm-test-run",
            "project": config["project"],
            "config": config,
            "phase_results": {},
            "attempts": {},
            "feedback": {},
            "reviews": [],
            "events": [],
            "artifacts": [],
            "aborted": False,
        }
        with patch.dict(os.environ, env, clear=False):
            result = build_graph().invoke(initial)

        expected_roles = {
            "executive_producer",
            "creative_director",
            "writer_storyboard",
            "asset_curator_rationale",
            "director",
            "review_board",
        }
        self.assertEqual(set(FakeChatHandler.roles), expected_roles)
        executive_upstream = FakeChatHandler.payloads[
            "executive_producer"
        ]["approved_upstream_context"]
        self.assertEqual(
            executive_upstream["project"]["theme"],
            stale_theme,
        )
        for role in expected_roles - {"executive_producer"}:
            upstream = FakeChatHandler.payloads[role][
                "approved_upstream_context"
            ]
            self.assertNotIn("project", upstream)
            self.assertNotIn(
                stale_theme,
                json.dumps(upstream, ensure_ascii=False),
            )
            if "project_brief" in upstream:
                self.assertNotIn(
                    "source_project",
                    upstream["project_brief"],
                )
        for role in expected_roles:
            phase = (
                "asset_curator"
                if role == "asset_curator_rationale"
                else role
            )
            llm = result["phase_results"][phase]["llm"]
            self.assertEqual(llm["provider"], "openai_compatible")
            self.assertEqual(llm["model"], "fake-model")
            self.assertEqual(llm["usage"]["total_tokens"], 30)

        concept = result["phase_results"]["creative_director"]["data"]
        self.assertEqual(
            concept["inherited_success_criteria"],
            ["北九州固有の魅力が伝わる"],
        )
        self.assertEqual(
            concept["success_criteria"],
            [
                "北九州固有の魅力が伝わる",
                "映像全体の色調と動きを統一する",
            ],
        )

        storyboard = result["phase_results"]["writer_storyboard"]["data"]
        self.assertEqual(
            sum(cut["seconds"] for cut in storyboard["cuts"]),
            30,
        )
        self.assertTrue(storyboard["duration_adjustment"]["applied"])
        self.assertEqual(
            storyboard["duration_adjustment"]["method"],
            "proportional_scale",
        )
        self.assertAlmostEqual(
            storyboard["duration_adjustment"]["scale_factor"],
            30 / 29,
            places=6,
        )

        self.assertEqual(storyboard["narration_language"], "ja")
        self.assertTrue(storyboard["narration_adjustments"])
        self.assertNotRegex(
            storyboard["cuts"][0]["narration"],
            r"[A-Za-z]",
        )

        curator_upstream = FakeChatHandler.payloads[
            "asset_curator_rationale"
        ][
            "approved_upstream_context"
        ]
        final_selections = curator_upstream["final_selections"]
        self.assertTrue(final_selections)
        for item in final_selections:
            candidate = item["selected_asset"]
            self.assertNotIn("source_url", candidate)
            self.assertNotIn("detail_url", candidate)
            self.assertNotIn("local_path", candidate)
            self.assertNotIn("sha256", candidate)
            self.assertIn("eligible_cut_ids", candidate)
            self.assertIn("scores_by_cut", candidate)

        assignments = result["phase_results"]["asset_curator"]["data"][
            "asset_assignments"
        ]
        self.assertEqual(len(assignments), 2)
        for item in assignments:
            self.assertIn(
                item["cut_id"],
                item["primary"]["eligible_cut_ids"],
            )
            self.assertEqual(
                item["primary"]["rationale_source"],
                "llm",
            )
            self.assertEqual(
                item["primary"]["selection_reason_source"],
                "deterministic",
            )
        shots = result["phase_results"]["director"]["data"]["shots"]
        self.assertEqual(
            [shot["asset"]["asset_id"] for shot in shots],
            [item["primary"]["asset_id"] for item in assignments],
        )

        final_video_path = Path(result["final_output"])
        self.assertTrue(final_video_path.exists())
        provenance_path = Path(
            result["phase_results"]["provenance"]["data"][
                "provenance"
            ]
        )
        self.assertNotIn(
            "test-secret-never-persist",
            provenance_path.read_text(encoding="utf-8"),
        )

    def test_review_feedback_reaches_llm_and_changes_artifact(self) -> None:
        config = yaml.safe_load(
            (ROOT / "config_llm.yaml").read_text(encoding="utf-8")
        )
        config["project"] = {
            "target_award": "夜景賞",
            "theme": "北九州の夜景を紹介する",
            "target_duration_seconds": 30,
        }
        feedback = "北九州への国内旅行を検討している人"
        state = {
            "run_id": "feedback-test",
            "project": config["project"],
            "config": config,
            "phase_results": {
                "executive_producer": {
                    "data": copy.deepcopy(ROLE_OUTPUTS["executive_producer"])
                }
            },
            "attempts": {"executive_producer": 1},
            "feedback": {"executive_producer": feedback},
            "reviews": [],
            "events": [],
            "artifacts": [],
        }
        env = {
            "AGEWEC_LLM_ENABLED": "true",
            "AGEWEC_LLM_PROVIDER": "openai_compatible",
            "AGEWEC_LLM_BASE_URL": (
                f"http://127.0.0.1:{self.server.server_port}/v1"
            ),
            "AGEWEC_LLM_API_KEY": "test-secret-never-persist",
            "AGEWEC_LLM_MODEL": "fake-model",
            "AGEWEC_LLM_STRUCTURED_OUTPUT_MODE": "prompt",
            "AGEWEC_LLM_TOKEN_PARAMETER": "max_tokens",
            "AGEWEC_LLM_MAX_RETRIES": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            update = nodes_llm.executive_producer(state)

        result = update["phase_results"]["executive_producer"]
        self.assertEqual(
            FakeChatHandler.payloads["executive_producer"]["review_feedback"],
            feedback,
        )
        self.assertEqual(result["data"]["audience"], feedback)
        self.assertEqual(
            result["feedback_status"],
            "delivered_to_llm_pending_human_verification",
        )
        self.assertEqual(
            result["feedback_application_evidence"],
            "output_changed",
        )

    def test_japanese_narration_is_shortened_to_cut_allowance(self) -> None:
        normalized, reasons = _normalize_japanese_narration(
            {
                "visual_role": "climax",
                "time_of_day": "night",
                "narration": (
                    "光に包まれた北九州の街並みと人々の営みが、"
                    "未来へ続く壮大な物語を静かに描き出します。"
                ),
            },
            allowance=16,
        )
        self.assertLessEqual(len(normalized), 16)
        self.assertIn("duration_fit_shortened", reasons)
        self.assertNotRegex(normalized, r"[A-Za-z]")

    def test_asset_candidate_payload_omits_provenance_fields(self) -> None:
        compact = _compact_asset_candidates_for_llm(
            [
                {
                    "asset_id": "asset-001",
                    "title": "皿倉山夜景",
                    "genres": ["イルミネーション・夜景"],
                    "areas": ["八幡東区"],
                    "time_of_day": "night",
                    "visual_roles": ["climax"],
                    "target_award_match": True,
                    "eligible_cut_ids": [3],
                    "scores_by_cut": {"3": 13},
                    "source_url": "https://example.invalid/image.jpg",
                    "detail_url": "https://example.invalid/detail",
                    "local_path": "/large/local/path/image.jpg",
                    "file_size_bytes": 123456,
                    "sha256": "a" * 64,
                    "acquired_at": "2026-07-31T00:00:00Z",
                }
            ]
        )
        self.assertEqual(compact[0]["asset_id"], "asset-001")
        self.assertEqual(compact[0]["scores_by_cut"], {"3": 13})
        for omitted in (
            "source_url",
            "detail_url",
            "local_path",
            "file_size_bytes",
            "sha256",
            "acquired_at",
        ):
            self.assertNotIn(omitted, compact[0])

    def test_asset_id_zero_padding_is_canonicalized(self) -> None:
        self.assertEqual(_canonical_asset_id("asset-6"), "asset-006")
        self.assertEqual(_canonical_asset_id("asset-06"), "asset-006")
        self.assertEqual(_canonical_asset_id("asset-006"), "asset-006")
        self.assertEqual(_canonical_asset_id("ASSET-6"), "asset-006")
        self.assertEqual(
            _canonical_asset_id("not-an-asset"),
            "not-an-asset",
        )

    def test_asset_selection_is_deterministic_and_target_retry_rotates(
        self,
    ) -> None:
        candidates = [
            {
                "asset_id": f"asset-{index:03d}",
                "title": title,
                "genres": ["観光スポット"],
                "areas": ["小倉"],
                "local_path": f"/tmp/asset-{index:03d}.jpg",
                "local_available": True,
                "time_of_day": "day_or_unspecified",
                "visual_roles": ["opening"],
                "target_award_match": True,
                "eligible_cut_ids": [1],
                "scores_by_cut": {"1": score},
            }
            for index, title, score in (
                (1, "候補A", 12),
                (2, "候補B", 10),
                (3, "候補C", 8),
            )
        ]
        state = {
            "run_id": "asset-ranker-test",
            "project": {"target_award": "観光賞"},
            "config": {
                "llm": {"enabled": False},
                "assets": {
                    "shortlist_per_cut": 8,
                    "alternatives_per_cut": 2,
                },
            },
            "phase_results": {
                "writer_storyboard": {
                    "data": {
                        "cuts": [
                            {
                                "id": 1,
                                "name": "導入",
                                "scene": "昼の小倉",
                                "seconds": 3,
                                "time_of_day": "day",
                                "visual_role": "opening",
                                "location": "小倉",
                                "subject": "街",
                            }
                        ]
                    }
                }
            },
            "attempts": {},
            "feedback": {},
            "review_context": {},
            "events": [],
            "artifacts": [],
        }
        with (
            patch.object(
                asset_role,
                "_asset_candidates",
                return_value=candidates,
            ),
            patch.object(
                asset_role,
                "_shortlist_candidates",
                return_value=candidates,
            ),
            patch.object(
                asset_role.deterministic,
                "_load_catalog",
                return_value={"source": "test catalog"},
            ),
        ):
            first = nodes_llm.asset_curator(state)
            first_data = first["phase_results"]["asset_curator"]["data"]
            self.assertEqual(
                first_data["asset_assignments"][0]["primary"]["asset_id"],
                "asset-001",
            )
            self.assertTrue(first_data["rationale_fallback_used"])

            retry_state = {
                **state,
                **first,
                "feedback": {
                    "asset_curator": "この素材は合わないので別候補へ",
                },
                "review_context": {
                    "asset_curator": {"target_cut_id": 1},
                },
            }
            second = nodes_llm.asset_curator(retry_state)
            second_data = second["phase_results"]["asset_curator"]["data"]
            self.assertEqual(
                second_data["asset_assignments"][0]["primary"]["asset_id"],
                "asset-002",
            )
            self.assertEqual(
                second_data["asset_assignments"][0]["primary"][
                    "selection_source"
                ],
                "retry_next_candidate",
            )

            explicit_state = {
                **retry_state,
                **second,
                "feedback": {
                    "asset_curator": "asset-3へ変更してください",
                },
            }
            explicit = nodes_llm.asset_curator(explicit_state)
            explicit_data = explicit["phase_results"]["asset_curator"]["data"]
            self.assertEqual(
                explicit_data["asset_assignments"][0]["primary"]["asset_id"],
                "asset-003",
            )
            self.assertEqual(
                explicit_data["asset_assignments"][0]["primary"][
                    "selection_source"
                ],
                "explicit_feedback",
            )

    def test_cut_durations_are_forced_to_target_by_proportional_scale(
        self,
    ) -> None:
        cuts, adjustment = _rescale_cut_durations(
            [
                {"id": 1, "seconds": 4.1},
                {"id": 2, "seconds": 5.2},
                {"id": 3, "seconds": 6.0},
                {"id": 4, "seconds": 5.0},
                {"id": 5, "seconds": 7.0},
            ],
            target_seconds=30.0,
            warning_threshold_seconds=2.0,
        )
        self.assertAlmostEqual(
            sum(float(cut["seconds"]) for cut in cuts),
            30.0,
            places=6,
        )
        self.assertAlmostEqual(
            adjustment["scale_factor"],
            30.0 / 27.3,
            places=6,
        )
        self.assertTrue(adjustment["large_adjustment_warning"])
        ratios = [
            change["adjusted_seconds"] / change["original_seconds"]
            for change in adjustment["cut_changes"][:-1]
        ]
        for ratio in ratios:
            self.assertAlmostEqual(
                ratio,
                adjustment["scale_factor"],
                places=5,
            )


class LanguagePolicyTest(unittest.TestCase):
    """出力言語ポリシーが全ロールの system prompt に載ることを検証する。

    日本語を既定にしつつ、(1) 動画生成モデルへ渡すプロンプトは英語、
    (2) 下流コードが値で分岐する機械語彙は不変、という2つの例外を守らせる。
    """

    def setUp(self) -> None:
        from agewec_v2.llm.role_runner import RoleRunner

        config = yaml.safe_load(
            (ROOT / "config_llm.yaml").read_text(encoding="utf-8")
        )
        self.runner = RoleRunner(config)

    def _prompt(self, role: str) -> str:
        return self.runner._system_prompt(role, ROLE_SCHEMAS[role])

    def test_every_role_receives_the_japanese_directive(self) -> None:
        for role in ROLE_SCHEMAS:
            with self.subTest(role=role):
                prompt = self._prompt(role)
                self.assertIn("LANGUAGE POLICY", prompt)
                self.assertIn("日本語", prompt)

    def test_generation_prompts_stay_english(self) -> None:
        """Runwayへ送る positive/negative prompt は英語指定であること。

        役割定義を日本語化した後も、この例外だけは英語指定のまま残す必要が
        ある。実際に日本語のプロンプトがRunwayへ送られていた実績があるため、
        指示が最優先（RULE A）として置かれていることも確認する。
        """
        prompt = self._prompt("director")
        for field in ENGLISH_PROMPT_FIELDS:
            self.assertIn(field, prompt)
        rule_a = prompt.split("RULE A")[1].split("RULE B")[0]
        self.assertIn("English", rule_a)
        self.assertIn("positive_prompt", rule_a)
        self.assertIn("never in Japanese", rule_a)
        # 他の規則より優先されると明示されていること
        self.assertIn("overrides", rule_a)

    def test_machine_tokens_are_protected_from_translation(self) -> None:
        """翻訳されると下流の照合・分岐が壊れるフィールドを守る。"""
        prompt = self._prompt("writer_storyboard")
        for field in MACHINE_TOKEN_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, prompt)
        self.assertIn("never translate", prompt.lower())

    def test_policy_precedes_the_schema_block(self) -> None:
        """スキーマ提示より前に置き、指示として読ませる。"""
        prompt = self._prompt("creative_director")
        self.assertLess(
            prompt.index("LANGUAGE POLICY"),
            prompt.index("JSON Schema"),
        )

    def test_role_prompt_is_still_included(self) -> None:
        """言語ポリシー追加で役割定義を潰していないこと。

        役割定義（prompts/<role>.md）は日本語で保守する方針なので、
        英語の固定文言ではなくファイルの中身と一致するかで確認する。
        """
        for role in ("director", "creative_director", "writer_storyboard"):
            with self.subTest(role=role):
                source = (
                    ROOT / "agewec_v2" / "prompts" / f"{role}.md"
                ).read_text(encoding="utf-8").strip()
                self.assertIn(source, self._prompt(role))

    def test_role_prompts_are_written_in_japanese(self) -> None:
        """人間が読んで直せるよう、役割定義は日本語で保守する。"""
        for role in ROLE_SCHEMAS:
            with self.subTest(role=role):
                text = (
                    ROOT / "agewec_v2" / "prompts" / f"{role}.md"
                ).read_text(encoding="utf-8")
                japanese = sum(
                    1 for c in text
                    if "぀" <= c <= "ヿ" or "一" <= c <= "鿿"
                )
                self.assertGreater(
                    japanese, 50, f"{role}.md が日本語で書かれていない"
                )


if __name__ == "__main__":
    unittest.main()
