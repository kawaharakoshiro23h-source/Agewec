import unittest

from agewec_v2.nodes_runtime import select_video_shots


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.shots = [
            {"id": 1, "media_strategy": "video"},
            {"id": 2, "media_strategy": "still"},
            {"id": 3, "media_strategy": "video"},
            {"id": 4, "media_strategy": "video"},
        ]

    def test_first_video_only_by_default(self):
        selected, deferred = select_video_shots(
            self.shots,
            max_video_cuts=1,
        )
        self.assertEqual([shot["id"] for shot in selected], [1])
        self.assertEqual([shot["id"] for shot in deferred], [3, 4])

    def test_specific_cut_can_be_selected(self):
        selected, deferred = select_video_shots(
            self.shots,
            max_video_cuts=1,
            requested_cut_ids=["4"],
        )
        self.assertEqual([shot["id"] for shot in selected], [4])
        self.assertEqual([shot["id"] for shot in deferred], [1, 3])


if __name__ == "__main__":
    unittest.main()
