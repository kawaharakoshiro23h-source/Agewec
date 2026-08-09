"""レポートのレイアウト崩れを防ぐ回帰テスト。

`.actual-row` は `grid-template-columns:180px 1fr` の2列グリッド。
カードを直下に並べると「見出し・カード1」で1行目が埋まり、
カード2以降が180pxの狭い列へ送られて潰れる（Cut1は正常、Cut2から崩れる）。
DOMの構造は同一なので目視でしか気づけず、実際に見落とした不具合。
行の子要素が常に2つであることを機械的に固定する。
"""
from __future__ import annotations

import unittest

from lxml import html as LH

from agewec_v2.pipeline_runtime import _phase_visual_cards


def _state(cut_count: int) -> dict:
    shots = [
        {
            "id": i,
            "asset": {
                "asset_id": f"asset-{i:03d}",
                "title": f"素材{i}",
                "local_path": f"/tmp/asset-{i:03d}.jpg",
            },
            "camera_motion": "slow push-in",
            "positive_prompt": f"prompt {i}",
            "negative_prompt": "",
            "rationale": f"理由 {i}",
        }
        for i in range(1, cut_count + 1)
    ]
    return {
        "run_id": "run-layout-test",
        "phase_results": {
            "director": {"data": {"shots": shots}},
            "writer_storyboard": {"data": {"cuts": [
                {"id": i, "name": f"カット{i}", "seconds": 5.0,
                 "time_of_day": "night"}
                for i in range(1, cut_count + 1)
            ]}},
        },
        "production_requests": {
            str(i): {"seed": 100 + i, "width": 1280, "height": 720}
            for i in range(1, cut_count + 1)
        },
        "production_artifacts": {
            str(i): {"path": f"/tmp/cut_{i:02d}.mp4", "attempt": 1}
            for i in range(1, cut_count + 1)
        },
        "cut_qa_results": {
            str(i): {"verdict": "pass", "issues": [],
                     "technical": {"width": 1280, "height": 720, "fps": 24.0}}
            for i in range(1, cut_count + 1)
        },
        "cut_attempts": {str(i): 1 for i in range(1, cut_count + 1)},
    }


class ActualRowLayoutTest(unittest.TestCase):
    def _row(self, phase: str, cut_count: int):
        markup = _phase_visual_cards(phase, _state(cut_count))
        self.assertTrue(markup, f"{phase} のカードが生成されていない")
        return LH.fragment_fromstring(markup)

    def test_row_has_exactly_two_grid_children(self) -> None:
        """見出し＋カード置き場の2つ。カードが直下に来ると崩れる。"""
        for phase in ("director", "image_video_production"):
            for cut_count in (1, 2, 6):
                with self.subTest(phase=phase, cuts=cut_count):
                    row = self._row(phase, cut_count)
                    self.assertEqual(
                        [child.tag for child in row],
                        ["h4", "div"],
                        "actual-row の直下は見出しとカード置き場だけにする",
                    )

    def test_all_cards_live_inside_one_container(self) -> None:
        for phase in ("director", "image_video_production"):
            with self.subTest(phase=phase):
                row = self._row(phase, 6)
                stack = row[1]
                cards = [
                    d for d in stack
                    if "background:#fafbfc" in (d.get("style") or "")
                ]
                self.assertEqual(len(cards), 6)
                # カードが行の直下に漏れていないこと
                self.assertEqual(
                    sum(
                        1 for d in row
                        if "background:#fafbfc" in (d.get("style") or "")
                    ),
                    0,
                )

    def test_container_can_shrink(self) -> None:
        """min-width:0 が無いとgrid内で画像が列幅を押し広げる。"""
        row = self._row("director", 2)
        self.assertIn("card-stack", row[1].get("class", ""))


if __name__ == "__main__":
    unittest.main()
