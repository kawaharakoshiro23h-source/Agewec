"""動画バックエンドの共通契約・capabilities・課金ガードのテスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agewec_v2.backends import (
    UnsupportedDurationError,
    Capabilities,
    VideoBudgetExceededError,
    VideoCostGuard,
    estimate_run_cost,
    resolve_backend,
    to_video_request,
)
from agewec_v2.media_tools import generate_mock_video


class CapabilitiesTest(unittest.TestCase):
    """尺の切り上げはモデル単位。プロバイダ単位で固定しない。"""

    def setUp(self) -> None:
        self.veo = Capabilities(
            model="veo3.1_fast",
            allowed_seconds=(4.0, 6.0, 8.0),
            cost_per_second_usd=0.10,
        )
        self.free = Capabilities(model="ltx", allowed_seconds=())

    def test_rounds_up_to_allowed_length(self) -> None:
        self.assertEqual(self.veo.resolve_seconds(5.0), 6.0)
        self.assertEqual(self.veo.resolve_seconds(6.0), 6.0)
        self.assertEqual(self.veo.resolve_seconds(7.5), 8.0)

    def test_never_rounds_down(self) -> None:
        """短くすると Phase 08 で尺不足エラーになるため、切り下げない。"""
        self.assertGreaterEqual(self.veo.resolve_seconds(4.1), 4.1)

    def test_exceeding_max_raises_instead_of_shortening(self) -> None:
        """上限超過は「最大尺へ切り詰め」ではなく失敗させる。

        黙って短くすると Phase 08 で尺不足エラーになり、原因が分かりにくい。
        カット分割か別モデル選択を促すため、生成前にここで弾く。
        """
        with self.assertRaises(UnsupportedDurationError):
            self.veo.resolve_seconds(20.0)

    def test_estimate_also_refuses_unsupported_duration(self) -> None:
        with self.assertRaises(UnsupportedDurationError):
            self.veo.estimate_cost(20.0)

    def test_free_model_accepts_any_length(self) -> None:
        self.assertEqual(self.free.resolve_seconds(5.3), 5.3)

    def test_cost_uses_billed_not_requested_seconds(self) -> None:
        """6秒要求→8秒課金のようなケースを正しく見積もる。"""
        self.assertAlmostEqual(self.veo.estimate_cost(7.5), 8.0 * 0.10)

    def test_minimum_charge_is_applied(self) -> None:
        """短尺でもモデル固有の最低課金を下回って見積もらない。"""
        caps = Capabilities(
            model="minimum-charge-model",
            allowed_seconds=(2.0, 4.0),
            cost_per_second_usd=0.10,
            minimum_cost_usd=0.64,
        )
        self.assertAlmostEqual(caps.estimate_cost(2.0), 0.64)
        self.assertAlmostEqual(caps.estimate_cost(4.0), 0.64)


class CostGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_allows_within_limit(self) -> None:
        guard = VideoCostGuard(self.ledger, limit_usd=10.0)
        self.assertTrue(guard.check(3.0).allowed)

    def test_blocks_when_projected_exceeds_limit(self) -> None:
        """実課金累計 + 次回見積 > 上限 なら送信させない。"""
        guard = VideoCostGuard(self.ledger, limit_usd=10.0)
        guard.record(
            cut_id=1, provider="runway", model="veo3.1_fast",
            cost_usd=9.0, billed_seconds=60,
        )
        status = guard.check(2.0)
        self.assertFalse(status.allowed)
        self.assertAlmostEqual(status.projected_usd, 11.0)
        with self.assertRaises(VideoBudgetExceededError):
            guard.ensure(2.0)

    def test_retries_accumulate_actual_cost(self) -> None:
        """再生成を繰り返すと実課金が積み上がり、いずれ止まる。"""
        guard = VideoCostGuard(self.ledger, limit_usd=5.0)
        for _ in range(4):
            if guard.check(1.2).allowed:
                guard.record(
                    cut_id=1, provider="runway", model="veo3.1_fast",
                    cost_usd=1.2, billed_seconds=8,
                )
        self.assertFalse(guard.check(1.2).allowed)
        self.assertLessEqual(guard.spent_usd, 5.0)

    def test_records_estimate_and_actual_separately(self) -> None:
        """切り上げ課金があるため、見積と実額は別に持つ。"""
        guard = VideoCostGuard(self.ledger, limit_usd=10.0)
        guard.record(
            cut_id=1, provider="runway", model="veo3.1_fast",
            cost_usd=1.20, billed_seconds=8, estimated_usd=0.90,
        )
        import json
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertAlmostEqual(ledger["spent_usd"], 1.20)
        self.assertAlmostEqual(ledger["estimated_usd"], 0.90)
        self.assertEqual(ledger["generations"][0]["billed_seconds"], 8)

    def test_run_estimate_for_human_approval(self) -> None:
        """H2 で人間に提示する全体見積。"""
        caps = Capabilities(
            model="veo3.1_fast",
            allowed_seconds=(4.0, 6.0, 8.0),
            cost_per_second_usd=0.10,
        )
        summary = estimate_run_cost(
            caps, [{"id": 1, "seconds": 5.0}, {"id": 2, "seconds": 7.0}]
        )
        # 5秒→6秒課金、7秒→8秒課金
        self.assertAlmostEqual(summary["total_usd"], (6.0 + 8.0) * 0.10)
        self.assertEqual(summary["cuts"][0]["billed_seconds"], 6.0)


class LocalBackendTest(unittest.TestCase):
    """既存バックエンドは無料であり、ガードの対象外であること。"""

    @staticmethod
    def _mock(tmp: str):
        return resolve_backend(
            "mock",
            generate_mock_video=generate_mock_video,
            output_path_for=lambda cut, attempt: (
                Path(tmp) / f"cut{cut:02d}_attempt{attempt:02d}.mp4"
            ),
        )

    def test_local_backends_are_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mock = self._mock(tmp)
        comfy = resolve_backend("comfy", generate_comfy=lambda s, r: {})
        self.assertEqual(mock.capabilities().cost_per_second_usd, 0.0)
        self.assertEqual(comfy.capabilities().cost_per_second_usd, 0.0)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_backend("unknown")

    def test_all_backends_share_one_signature(self) -> None:
        """全Adapterが generate(VideoRequest) だけを受け取ること。

        呼び出し側にバックエンド分岐を残さないための不変条件。
        """
        import inspect

        with tempfile.TemporaryDirectory() as tmp:
            adapters = [
                self._mock(tmp),
                resolve_backend("comfy", generate_comfy=lambda s, r: {}),
            ]
        for adapter in adapters:
            params = list(
                inspect.signature(adapter.generate).parameters
            )
            self.assertEqual(
                params, ["request"], f"{adapter.provider} のシグネチャが不一致"
            )

    def test_mock_backend_generates_requested_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._mock(tmp)
            result = backend.generate(
                to_video_request(
                    {
                        "cut_id": 1,
                        "actual_seconds": 1.0,
                        "width": 64,
                        "height": 64,
                        "fps": 8,
                    },
                    attempt=2,
                )
            )
            self.assertTrue(Path(result.output_path).exists())
            self.assertIn("attempt02", result.output_path)
        self.assertEqual(result.provider, "mock")
        self.assertEqual(result.cost_usd, 0.0)
        self.assertEqual(result.requested_seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
