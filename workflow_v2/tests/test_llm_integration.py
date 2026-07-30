from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from agewec_v2.graph import build_graph


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
        "success_criteria": ["北九州固有の魅力が伝わる"],
    },
    "writer_storyboard": {
        "total_seconds": 30,
        "cuts": [
            {
                "id": 1,
                "name": "導入",
                "scene": "北九州の夜景",
                "narration": "光の街へ。",
                "seconds": 15,
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
    "asset_curator": {
        "selections": [
            {
                "cut_id": 1,
                "primary": {
                    "asset_id": "asset-001",
                    "reason": "導入に合う",
                },
                "alternatives": [],
            },
            {
                "cut_id": 2,
                "primary": {
                    "asset_id": "asset-002",
                    "reason": "産業景観に合う",
                },
                "alternatives": [],
            },
        ],
        "missing_requirements": [],
    },
    "director": {
        "shots": [
            {
                "cut_id": 1,
                "asset_id": "asset-001",
                "positive_prompt": "Kitakyushu night view, slow push in",
                "negative_prompt": "",
                "camera_motion": "slow push in",
                "motion_intensity": "subtle",
                "rationale": "導入に奥行きを与える",
                "camera_intent_alignment": "安定した導入",
                "deviation_reason": None,
            },
            {
                "cut_id": 2,
                "asset_id": "asset-002",
                "positive_prompt": "Kitakyushu industrial lights, slow pan",
                "negative_prompt": "",
                "camera_motion": "slow pan",
                "motion_intensity": "subtle",
                "rationale": "街の広がりを見せる",
                "camera_intent_alignment": "終盤へ穏やかに導く",
                "deviation_reason": None,
            },
        ],
        "continuity_checks": ["deep blueとamberを維持"],
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

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        user_content = request["messages"][-1]["content"]
        role = json.loads(user_content)["role"]
        self.__class__.roles.append(role)
        output = ROLE_OUTPUTS[role]
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
    @classmethod
    def setUpClass(cls) -> None:
        FakeChatHandler.roles = []
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
        config["production"]["backend"] = "mock"
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
            "asset_curator",
            "director",
            "review_board",
        }
        self.assertEqual(set(FakeChatHandler.roles), expected_roles)
        for role in expected_roles:
            llm = result["phase_results"][role]["llm"]
            self.assertEqual(llm["provider"], "openai_compatible")
            self.assertEqual(llm["model"], "fake-model")
            self.assertEqual(llm["usage"]["total_tokens"], 30)

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


if __name__ == "__main__":
    unittest.main()
