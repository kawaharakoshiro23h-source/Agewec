"""RunwayBackend のテスト（偽Runwayサーバを立てるため APIキー不要）。

実APIを叩かずに、次を検証する:
    アップロード → 生成依頼 → ポーリング → ダウンロード → VideoResult
    モデル別の尺切り上げ・費用計算・音声抑止・失敗時の扱い
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from agewec_v2.backends import to_video_request
from agewec_v2.backends.base import UnsupportedDurationError
from agewec_v2.backends.runway import RunwayBackend, RunwayError

MODELS = {
    "veo3.1_fast": {
        "allowed_seconds": [4, 6, 8],
        "supports_seed": True,
        "supports_negative_prompt": True,
        "has_native_audio": True,
        "request_audio": False,
        "cost_per_second_usd": 0.10,
    },
    "gen4.5": {
        "allowed_seconds": list(range(2, 11)),
        "supports_seed": True,
        "supports_negative_prompt": False,
        "has_native_audio": False,
        "cost_per_second_usd": 0.12,
    },
    "seedance2": {
        "allowed_seconds": list(range(4, 16)),
        "supports_seed": True,
        "supports_negative_prompt": False,
        "has_native_audio": True,
        "cost_per_second_usd": 0.36,
    },
}


class FakeRunwayHandler(BaseHTTPRequestHandler):
    """最小限の偽Runway API。"""

    payloads: list[dict] = []
    upload_preparations: list[dict] = []
    signed_uploads = 0
    poll_count = 0
    fail_task = False

    def log_message(self, *args):  # noqa: D102 - テスト出力を汚さない
        return

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if self.path == "/v1/uploads":
            request = json.loads(raw)
            FakeRunwayHandler.upload_preparations.append(request)
            self._json(
                200,
                {
                    "uploadUrl": (
                        "http://127.0.0.1:%d/signed-upload"
                        % self.server.server_port
                    ),
                    "fields": {
                        "key": "ephemeral/source.jpg",
                        "policy": "fake-policy",
                    },
                    "runwayUri": "runway://ephemeral/source.jpg",
                },
            )
            return
        if self.path == "/signed-upload":
            self.assert_multipart_upload(raw)
            FakeRunwayHandler.signed_uploads += 1
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/v1/image_to_video":
            FakeRunwayHandler.payloads.append(json.loads(raw))
            self._json(200, {"id": "task-123"})
            return
        self._json(404, {"error": "not found"})

    def assert_multipart_upload(self, raw: bytes) -> None:
        """署名付きURLへfieldsとファイル本体が送られたことを確認する。"""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise AssertionError(f"not multipart: {content_type}")
        for expected in (b"ephemeral/source.jpg", b"fake-policy", b"fake-image"):
            if expected not in raw:
                raise AssertionError(f"missing multipart value: {expected!r}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/tasks/"):
            FakeRunwayHandler.poll_count += 1
            if FakeRunwayHandler.fail_task:
                self._json(200, {"status": "FAILED", "failure": "boom"})
                return
            # 1回目は実行中、2回目で完了（ポーリングの検証）
            if FakeRunwayHandler.poll_count < 2:
                self._json(200, {"status": "RUNNING"})
                return
            self._json(
                200,
                {
                    "status": "SUCCEEDED",
                    "output": ["http://127.0.0.1:%d/out.mp4" % self.server.server_port],
                },
            )
            return
        if self.path == "/out.mp4":
            data = b"fake-mp4-content"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found"})


class RunwayBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRunwayHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeRunwayHandler.payloads = []
        FakeRunwayHandler.upload_preparations = []
        FakeRunwayHandler.signed_uploads = 0
        FakeRunwayHandler.poll_count = 0
        FakeRunwayHandler.fail_task = False
        self.tmp = tempfile.TemporaryDirectory()
        self.image = Path(self.tmp.name) / "source.jpg"
        self.image.write_bytes(b"fake-image")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _backend(self, model: str = "veo3.1_fast") -> RunwayBackend:
        return RunwayBackend(
            api_key="test-key",
            model=model,
            models=MODELS,
            output_path_for=lambda cut, attempt: (
                Path(self.tmp.name) / f"cut{cut:02d}_attempt{attempt:02d}.mp4"
            ),
            base_url=f"http://127.0.0.1:{self.port}",
            poll_interval=0.01,
            timeout=10,
            client=httpx.Client(timeout=10.0),
        )

    def _request(self, seconds: float = 5.0, **kwargs):
        raw = {
            "cut_id": 3,
            "image_path": str(self.image),
            "positive_prompt": "cinematic night view",
            "negative_prompt": "distorted architecture",
            "actual_seconds": seconds,
            "seed": 4242,
            **kwargs,
        }
        return to_video_request(raw, attempt=2)

    # ------------------------------------------------------------ 正常系
    def test_full_roundtrip_uploads_polls_and_downloads(self) -> None:
        result = self._backend().generate(self._request())
        self.assertTrue(Path(result.output_path).exists())
        self.assertEqual(result.provider, "runway")
        self.assertEqual(result.job_id, "task-123")
        self.assertGreaterEqual(FakeRunwayHandler.poll_count, 2)  # ポーリング
        self.assertIn("attempt02", result.output_path)
        self.assertEqual(
            FakeRunwayHandler.upload_preparations,
            [{"filename": "source.jpg", "type": "ephemeral"}],
        )
        self.assertEqual(FakeRunwayHandler.signed_uploads, 1)
        self.assertEqual(
            FakeRunwayHandler.payloads[0]["promptImage"],
            "runway://ephemeral/source.jpg",
        )

    def test_rounds_up_duration_and_bills_the_rounded_value(self) -> None:
        """5秒要求 → 6秒生成・6秒課金（短くはしない）。"""
        result = self._backend().generate(self._request(seconds=5.0))
        self.assertEqual(FakeRunwayHandler.payloads[0]["duration"], 6)
        self.assertEqual(result.requested_seconds, 5.0)
        self.assertEqual(result.billed_seconds, 6.0)
        self.assertAlmostEqual(result.cost_usd, 6.0 * 0.10)

    def test_requests_audio_disabled_when_model_supports_it(self) -> None:
        """音声内蔵モデルには audio=false を送る。"""
        self._backend().generate(self._request())
        self.assertIs(FakeRunwayHandler.payloads[0]["audio"], False)

    def test_omits_unsupported_fields_per_model(self) -> None:
        """negative prompt 非対応モデルには送らない。"""
        self._backend("gen4.5").generate(self._request(seconds=5.0))
        payload = FakeRunwayHandler.payloads[0]
        self.assertNotIn("negativePrompt", payload)
        self.assertNotIn("audio", payload)     # 音声非内蔵なので指定なし
        self.assertEqual(payload["model"], "gen4.5")
        self.assertEqual(payload["seed"], 4242)
        self.assertEqual(payload["duration"], 5)

    def test_seedance_omits_unverified_audio_and_negative_fields(self) -> None:
        """未確認フィールドを送って実APIで400になる事故を防ぐ。"""
        self._backend("seedance2").generate(self._request(seconds=5.0))
        payload = FakeRunwayHandler.payloads[0]
        self.assertNotIn("negativePrompt", payload)
        self.assertNotIn("audio", payload)
        self.assertEqual(payload["duration"], 5)

    def test_model_switch_changes_capabilities(self) -> None:
        veo = self._backend("veo3.1_fast").capabilities()
        gen = self._backend("gen4.5").capabilities()
        self.assertEqual(veo.resolve_seconds(5.0), 6.0)
        self.assertEqual(gen.resolve_seconds(5.0), 5.0)
        self.assertNotEqual(veo.cost_per_second_usd, gen.cost_per_second_usd)

    # ------------------------------------------------------------ 異常系
    def test_duration_over_model_limit_raises(self) -> None:
        with self.assertRaises(UnsupportedDurationError):
            self._backend().generate(self._request(seconds=30.0))

    def test_failed_task_raises(self) -> None:
        FakeRunwayHandler.fail_task = True
        with self.assertRaises(RunwayError):
            self._backend().generate(self._request())

    def test_unknown_model_raises(self) -> None:
        with self.assertRaises(RunwayError):
            self._backend("does-not-exist").capabilities()

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(RunwayError):
            RunwayBackend(
                api_key="",
                model="veo3.1_fast",
                models=MODELS,
                output_path_for=lambda c, a: "/tmp/x.mp4",
            )


if __name__ == "__main__":
    unittest.main()
