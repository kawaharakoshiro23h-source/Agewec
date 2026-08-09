"""人間が名指しした素材が、機械の候補外でも採用されることを検証する。

ショートリストは「機械の提案」であって、人間の指示を却下する根拠ではない。
上位N件に入らなかったという理由だけで拒否してはならない。一方で、
ローカルに実体が無い・利用条件を満たさない素材は、課金前にここで止める。
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from agewec_v2 import nodes_llm
from agewec_v2.roles import assets as asset_role


ROOT = Path(__file__).resolve().parents[1]

# cut 1 の条件。夕暮れ・中心部・主役。
CUT = {
    "id": 1,
    "name": "締め",
    "scene": "夕暮れの街",
    "narration": "光の先へ。",
    "seconds": 5.0,
    "media_requirement": "video_required",
    "time_of_day": "dusk",
    "visual_role": "climax",
    "location": "北九州市中心部",
    "subject": "夜景",
}


def _asset(
    asset_id: str,
    *,
    title: str,
    time_of_day: str,
    areas: list[str],
    local_path: str,
    local_available: bool = True,
    rights: str = "approved_for_agewec_submission",
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "title": title,
        "source_url": f"https://example.invalid/{asset_id}.jpg",
        "detail_url": "",
        "genres": ["夜景"],
        "areas": areas,
        "local_path": local_path,
        "local_available": local_available,
        "time_of_day": time_of_day,
        "visual_roles": ["夜景"],
        "target_award_match": True,
        "usage_scope": "agewec_submission",
        "rights_status": rights,
        "file_size_bytes": 1024,
        "sha256": "0" * 64,
        "acquired_at": "2026-08-01T00:00:00+00:00",
    }


class ExplicitAssetChoiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.config = yaml.safe_load(
            (ROOT / "configs/config_local.yaml").read_text(encoding="utf-8")
        )
        self.config["llm"] = {"enabled": False}
        # 候補枠を意図的に狭める。これが「候補外」を作る条件。
        self.config.setdefault("assets", {})["shortlist_per_cut"] = 3
        self.config["assets"]["alternatives_per_cut"] = 2

        self.catalogue: list[dict[str, Any]] = []
        # 上位に来る素材（夕暮れ・中心部）。
        # ショートリストには「同一エリア・同一タイトル接頭辞は各2件まで」という
        # 多様性キャップがある。エリア名と表題を散らさないと枠が埋まらず、
        # 本来候補外のはずの素材が繰り上がってしまう（実際に一度そうなった）。
        for index, (ward, label) in enumerate(
            [
                ("小倉北区", "小倉の夕景"),
                ("戸畑区", "戸畑の夕景"),
                ("八幡東区", "八幡の夕景"),
                ("若松区", "若松の夕景"),
                ("小倉南区", "南区の夕景"),
            ],
            start=1,
        ):
            path = tmp / f"good-{index}.jpg"
            path.write_bytes(b"\xff\xd8\xff")
            self.catalogue.append(
                _asset(
                    f"asset-{index:03d}",
                    title=label,
                    time_of_day="dusk",
                    areas=[f"{ward}・北九州市中心部"],
                    local_path=str(path),
                )
            )
        # 候補外になる素材（日中・別エリア）。実体はある。
        far = tmp / "far.jpg"
        far.write_bytes(b"\xff\xd8\xff")
        self.outside = _asset(
            "asset-017",
            title="関門海峡ミュージアム03",
            time_of_day="unknown_or_day",
            areas=["門司港レトロエリア"],
            local_path=str(far),
        )
        # ローカル未取得の素材
        self.missing_local = _asset(
            "asset-099",
            title="未取得の写真",
            time_of_day="dusk",
            areas=["北九州市中心部"],
            local_path="",
            local_available=False,
        )
        # ファイルが消えている素材
        self.deleted_file = _asset(
            "asset-098",
            title="消えた写真",
            time_of_day="dusk",
            areas=["北九州市中心部"],
            local_path=str(tmp / "does-not-exist.jpg"),
        )
        # 権利が通っていない素材
        self.no_rights = _asset(
            "asset-097",
            title="権利未確認の写真",
            time_of_day="dusk",
            areas=["北九州市中心部"],
            local_path=str(tmp / "good-1.jpg"),
            rights="unknown",
        )
        self.catalogue += [
            self.outside,
            self.missing_local,
            self.deleted_file,
            self.no_rights,
        ]

        self._real = asset_role._asset_candidates
        asset_role._asset_candidates = lambda state: copy.deepcopy(self.catalogue)

    def tearDown(self) -> None:
        asset_role._asset_candidates = self._real
        self._tmp.cleanup()

    def _state(self, feedback: str) -> dict[str, Any]:
        return {
            "run_id": "explicit-asset-test",
            "project": self.config["project"],
            "config": self.config,
            "phase_results": {
                "writer_storyboard": {
                    "phase": "writer_storyboard",
                    "status": "success",
                    "data": {"total_seconds": 5.0, "cuts": [dict(CUT)]},
                }
            },
            "attempts": {},
            "feedback": {"asset_curator": feedback},
            "review_context": {"asset_curator": {"target_cut_id": 1}},
            "reviews": [],
            "events": [],
            "artifacts": [],
            "aborted": False,
        }

    def _run(self, feedback: str) -> dict[str, Any]:
        result = nodes_llm.asset_curator(self._state(feedback))
        return result["phase_results"]["asset_curator"]

    # --- 前提の確認 -----------------------------------------------------

    def test_the_named_asset_really_is_outside_the_shortlist(self) -> None:
        """テストの前提が成立していること。

        asset-017 が実は候補内だったなら、以下のテストは何も証明しない。
        """
        short = nodes_llm._shortlist_candidates(
            [dict(CUT)], copy.deepcopy(self.catalogue), "夜景賞", 3
        )
        eligible = {
            item["asset_id"]
            for item in short
            if 1 in item.get("eligible_cut_ids", [])
        }
        self.assertNotIn("asset-017", eligible)

    # --- 本題 -----------------------------------------------------------

    def test_accepts_an_asset_outside_the_shortlist(self) -> None:
        result = self._run("asset-017の写真を利用してほしい")
        self.assertEqual(result["status"], "success", result.get("blocking_issues"))
        primary = result["data"]["asset_assignments"][0]["primary"]
        self.assertEqual(primary["asset_id"], "asset-017")
        self.assertEqual(primary["selection_source"], "explicit_feedback")

    def test_records_that_the_choice_came_from_a_human(self) -> None:
        """後から「なぜこの写真か」を追えること。"""
        result = self._run("asset-017の写真を利用してほしい")
        primary = result["data"]["asset_assignments"][0]["primary"]
        self.assertTrue(primary["outside_shortlist"])
        self.assertIn("人間の明示指定", primary["selection_reason"])
        self.assertIn("候補", primary["selection_reason"])
        self.assertTrue(
            any("明示指定" in str(w) for w in result.get("warnings", [])),
            result.get("warnings"),
        )

    def test_fills_in_the_eligibility_the_shortlist_never_assigned(self) -> None:
        """下流の照合が通るよう、適合情報を補っていること。

        `test_pipeline_1cut` の検証は eligible_cut_ids を見る。空のまま
        通すと検査が素通りしてしまうため、実スコアを入れて残す。
        """
        result = self._run("asset-017の写真を利用してほしい")
        primary = result["data"]["asset_assignments"][0]["primary"]
        self.assertIn(1, primary["eligible_cut_ids"])
        self.assertIn("1", primary["scores_by_cut"])
        self.assertIsInstance(primary["scores_by_cut"]["1"], int)

    def test_the_chosen_asset_is_not_repeated_in_alternatives(self) -> None:
        result = self._run("asset-017の写真を利用してほしい")
        assignment = result["data"]["asset_assignments"][0]
        alternative_ids = [a["asset_id"] for a in assignment["alternatives"]]
        self.assertNotIn("asset-017", alternative_ids)

    # --- 拒否し続けるべきもの -------------------------------------------

    def test_rejects_an_unknown_asset_id(self) -> None:
        result = self._run("asset-777を使って")
        self.assertEqual(result["status"], "error")
        self.assertIn("存在しません", " ".join(result["blocking_issues"]))

    def test_rejects_an_asset_that_was_never_downloaded(self) -> None:
        """課金してから失敗するより、選定の時点で止める。"""
        result = self._run("asset-099を使って")
        self.assertEqual(result["status"], "error")
        self.assertIn("ローカルに未取得", " ".join(result["blocking_issues"]))

    def test_rejects_an_asset_whose_file_is_gone(self) -> None:
        result = self._run("asset-098を使って")
        self.assertEqual(result["status"], "error")
        self.assertIn("見つかりません", " ".join(result["blocking_issues"]))

    def test_rejects_an_asset_without_clear_usage_rights(self) -> None:
        result = self._run("asset-097を使って")
        self.assertEqual(result["status"], "error")
        self.assertIn("利用条件", " ".join(result["blocking_issues"]))

    def test_still_requires_a_target_cut_id(self) -> None:
        """既存の安全策が壊れていないこと。"""
        state = self._state("asset-017を使って")
        state["review_context"]["asset_curator"] = {}
        result = nodes_llm.asset_curator(state)["phase_results"]["asset_curator"]
        self.assertEqual(result["status"], "error")
        self.assertIn("target_cut_id", " ".join(result["blocking_issues"]))

    # --- カット単位の一括指定 --------------------------------------------

    def test_assigns_every_cut_named_in_one_instruction(self) -> None:
        """1回の指示で複数カットを指定できること。

        以前は re.search で最初の1件しか読まず、残りは警告もなく
        捨てられていた。8カット書いても1カットしか反映されない。
        """
        state = self._state(
            "Cut1:Asset-001, Cut2 : Asset-017を使ってください。"
        )
        state["review_context"]["asset_curator"] = {}
        state["phase_results"]["writer_storyboard"]["data"]["cuts"] = [
            dict(CUT), {**CUT, "id": 2, "name": "2番目"},
        ]
        result = nodes_llm.asset_curator(state)["phase_results"]["asset_curator"]
        self.assertEqual(result["status"], "success", result.get("blocking_issues"))
        chosen = {
            a["cut_id"]: a["primary"]["asset_id"]
            for a in result["data"]["asset_assignments"]
        }
        self.assertEqual(chosen, {1: "asset-001", 2: "asset-017"})

    def test_bulk_assignment_needs_no_target_cut_id(self) -> None:
        """カット番号が文中にあるので、対象カットIDの入力は不要。"""
        state = self._state("Cut1:Asset-017")
        state["review_context"]["asset_curator"] = {}
        result = nodes_llm.asset_curator(state)["phase_results"]["asset_curator"]
        self.assertEqual(result["status"], "success", result.get("blocking_issues"))

    def test_rejects_a_cut_number_that_does_not_exist(self) -> None:
        """絵コンテにないカット番号は、黙って捨てずに知らせる。"""
        state = self._state("Cut1:Asset-001, Cut9:Asset-017")
        state["review_context"]["asset_curator"] = {}
        result = nodes_llm.asset_curator(state)["phase_results"]["asset_curator"]
        self.assertEqual(result["status"], "error")
        self.assertIn("9", " ".join(result["blocking_issues"]))

    def test_records_the_bulk_assignment_for_the_trail(self) -> None:
        state = self._state("Cut1:Asset-017")
        state["review_context"]["asset_curator"] = {}
        result = nodes_llm.asset_curator(state)["phase_results"]["asset_curator"]
        self.assertEqual(
            result["data"]["requested_asset_map"], {"1": "asset-017"}
        )

    def test_parses_the_exact_wording_a_human_typed(self) -> None:
        """利用者が実際に打った文字列がそのまま通ること。

        全角コロン・空白の揺れ・末尾の日本語を含む生の入力で検証する。
        """
        typed = (
            "Cut1:Asset-009, Cut2:Asset-076, Cut3 : Asset-041, "
            "Cut4 : Asset-003, Cut5 : Asset-018, Cut6 ：Asset-010, "
            "Cut7 : Asset-020, Cut8:Asset-006を使ってください。"
        )
        parsed = nodes_llm._requested_asset_map(typed)
        self.assertEqual(
            parsed,
            {
                1: "asset-009", 2: "asset-076", 3: "asset-041",
                4: "asset-003", 5: "asset-018", 6: "asset-010",
                7: "asset-020", 8: "asset-006",
            },
        )

    def test_a_later_line_overrides_an_earlier_one(self) -> None:
        """同じカットを書き直したら、後のほうを採用する。"""
        parsed = nodes_llm._requested_asset_map(
            "Cut1:Asset-001\nやっぱり Cut1:Asset-017"
        )
        self.assertEqual(parsed, {1: "asset-017"})

    def test_single_asset_wording_still_works(self) -> None:
        """従来の「asset-XXXを使って」＋対象カットIDも壊さない。"""
        result = self._run("asset-017の写真を利用してほしい")
        self.assertEqual(result["status"], "success", result.get("blocking_issues"))
        primary = result["data"]["asset_assignments"][0]["primary"]
        self.assertEqual(primary["asset_id"], "asset-017")

    # --- 既存動作の維持 --------------------------------------------------

    def test_without_feedback_the_ranker_still_decides(self) -> None:
        result = self._run("")
        primary = result["data"]["asset_assignments"][0]["primary"]
        self.assertNotEqual(primary["asset_id"], "asset-017")
        self.assertNotIn("outside_shortlist", primary)


if __name__ == "__main__":
    unittest.main()
