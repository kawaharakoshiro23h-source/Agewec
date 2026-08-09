"""差し戻しの対象カット指定が、どこまで効いてよいかの検証。

`target_cut_id` は「前回の完全な結果があるときだけ」差分更新に使える。
Directorで1カットだけ差し戻すと、その値が下流のPhase 05.5まで引き継がれる。
そこが初回実行だと絞り込む土台が無く、指定外のカットが未作成のまま落ちる。

実際の障害: run-d35ee139e1 で Cut8 のみRequestが作られ、
「ProductionRequestがないカット: 1, 2, 3, 4, 5, 6, 7」で停止した。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import agewec_v2.pipeline_runtime as runtime


def _shot(cut_id: int, image_path: str) -> dict:
    return {
        "id": cut_id,
        "name": f"カット{cut_id}",
        "seconds": 5.0,
        "media_requirement": "video_required",
        "positive_prompt": "cinematic night view",
        "negative_prompt": "",
        "camera_motion": "static",
        "motion_intensity": "static",
        "generation_mode": "image_to_video",
        "asset": {"local_path": image_path},
    }


def _state(directory: str, image_path: str, *, cut_ids, target_cut_id, existing):
    return {
        "run_id": "run-scope",
        "phase_results": {
            "director": {
                "data": {
                    "shots": [_shot(i, image_path) for i in cut_ids],
                    "targeted_revision_cut_id": target_cut_id,
                }
            }
        },
        "production_requests": existing,
        "production_artifacts": {},
        "cut_qa_results": {},
        "cut_results": {},
        "approved_cut_ids": [],
        "cut_attempts": {},
        "attempts": {},
        "feedback": {},
        "review_context": {},
        "events": [],
        "artifacts": [],
        "config": {
            "paths": {"work_dir": directory},
            "production": {
                "backend": "mock",
                "profile": "draft",
                "profiles": {
                    "draft": {
                        "width": 576, "height": 384,
                        "frames": 49, "steps": 20, "fps": 24,
                    }
                },
                "model_constraints": {
                    "frame_multiple": 8, "frame_offset": 1, "max_frames": 257,
                },
            },
        },
    }


class TargetedRevisionScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.image = str(Path(self.dir) / "src.jpg")
        Path(self.image).write_bytes(b"\xff\xd8\xff")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _requests(self, update) -> set[int]:
        return {
            int(cut_id)
            for cut_id in update.get("production_requests", {})
        }

    def test_builds_every_cut_when_nothing_was_built_before(self) -> None:
        """初回実行では、対象カット指定があっても全カット作る。

        これが今回の障害。Cut8だけ差し戻した状態で初めてこの工程に来ると、
        Cut1〜7のRequestが作られず生成に進めなかった。
        """
        update = runtime.support_video_creator(
            _state(self.dir, self.image,
                   cut_ids=[1, 2, 3], target_cut_id=3, existing={})
        )
        self.assertEqual(self._requests(update), {1, 2, 3})
        self.assertEqual(
            update["phase_results"]["support_video_creator"]["status"],
            "success",
        )

    def test_builds_only_the_target_when_all_cuts_already_exist(self) -> None:
        """前回の結果が揃っているなら、指定カットだけ作り直す。

        こちらが本来の意図。無駄な再構築と、承認済み内容の書き換えを防ぐ。
        """
        existing = {
            "1": {"cut_id": 1, "marker": "keep"},
            "2": {"cut_id": 2, "marker": "keep"},
            "3": {"cut_id": 3, "marker": "old"},
        }
        update = runtime.support_video_creator(
            _state(self.dir, self.image,
                   cut_ids=[1, 2, 3], target_cut_id=3, existing=existing)
        )
        built = update["production_requests"]
        self.assertEqual(built["1"]["marker"], "keep")
        self.assertEqual(built["2"]["marker"], "keep")
        self.assertNotIn("marker", built["3"])   # 3番だけ作り直された

    def test_builds_every_cut_when_the_previous_result_is_partial(self) -> None:
        """前回結果が一部しかなければ、絞り込まず全カット埋める。"""
        update = runtime.support_video_creator(
            _state(self.dir, self.image,
                   cut_ids=[1, 2, 3], target_cut_id=3,
                   existing={"1": {"cut_id": 1, "marker": "keep"}})
        )
        self.assertEqual(self._requests(update), {1, 2, 3})

    def test_no_target_means_all_cuts(self) -> None:
        update = runtime.support_video_creator(
            _state(self.dir, self.image,
                   cut_ids=[1, 2], target_cut_id=None, existing={})
        )
        self.assertEqual(self._requests(update), {1, 2})


if __name__ == "__main__":
    unittest.main()
