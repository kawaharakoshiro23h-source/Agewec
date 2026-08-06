"""監視モニタ（読み取り専用）のテスト。

この機能で最も重要な性質は「パイプラインに影響しないこと」なので、
    1. 書き込みを一切行わない
    2. runs_root の外へは出さない（配信するのはrun配下のみ）
    3. 実行途中（ゲートが揃っていない状態）でも壊れない
を中心に検証する。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from http.server import ThreadingHTTPServer
from functools import partial
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

from agewec_v2.monitor.reader import list_runs, read_run
from agewec_v2.monitor.server import MonitorHandler


def _build_run(root: Path, run_id: str, *, partial_run: bool = False) -> Path:
    """実際のディレクトリ構造を模した run を作る。"""
    run = root / run_id
    gates = run / "gates"
    gates.mkdir(parents=True)
    phases = ["executive_producer", "creative_director", "writer_storyboard"]
    if not partial_run:
        phases += ["asset_curator", "director", "support_video_creator"]
    for index, phase in enumerate(phases):
        (gates / f"{phase}_attempt_01.json").write_text(
            json.dumps({"phase": phase, "status": "success",
                        "summary": f"{phase} 完了"}, ensure_ascii=False),
            encoding="utf-8",
        )
        # mtime を1分ずつずらし、所要時間の算出を検証できるようにする
        stamp = time.time() - (len(phases) - index) * 60
        import os
        os.utime(gates / f"{phase}_attempt_01.json", (stamp, stamp))
    # 差し戻し（2回目）の痕跡
    (gates / "creative_director_attempt_02.json").write_text(
        json.dumps({"phase": "creative_director", "status": "success",
                    "summary": "2回目"}, ensure_ascii=False), encoding="utf-8")

    if not partial_run:
        cut = run / "cuts" / "cut_01"
        cut.mkdir(parents=True)
        (cut / "request.json").write_text(json.dumps({
            "cut_id": 1, "model": "gen4.5", "generation_mode": "image_to_video",
            "actual_seconds": 5.0, "seed": 42, "attempt": 1,
            "positive_prompt": "夜景", "camera_motion": "slow pan",
        }, ensure_ascii=False), encoding="utf-8")
        (cut / "qa.json").write_text(json.dumps({
            "verdict": "pass", "issues": []}, ensure_ascii=False), encoding="utf-8")
        (cut / "source.jpg").write_bytes(b"jpeg")
        (cut / "attempt_01.mp4").write_bytes(b"mp4")
        (run / "video_cost_ledger.json").write_text(
            json.dumps({"spent_usd": 0.6, "generations": [{"cut_id": 1}]}),
            encoding="utf-8")
    return run


class ReaderTest(unittest.TestCase):
    def test_lists_runs_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-old")
            time.sleep(0.01)
            _build_run(root, "run-new")
            runs = [r["run_id"] for r in list_runs(root)]
        self.assertEqual(runs[0], "run-new")

    def test_directories_without_gates_never_come_first(self) -> None:
        """work/runs には実験用の残骸も混ざる。

        ゲートが1つも無いディレクトリは、たとえ更新時刻が最も新しくても
        既定の表示対象にしない（実行中のrunを見に来た人が別物を見せられる）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-real")
            leftover = root / "scratch-dir"
            (leftover / "cuts" / "cut_01").mkdir(parents=True)
            time.sleep(0.01)
            (leftover / "cuts" / "cut_01" / "attempt_01.mp4").write_bytes(b"x")
            runs = list_runs(root)
        self.assertEqual(runs[0]["run_id"], "run-real")
        self.assertTrue(runs[0]["has_gates"])
        self.assertFalse(
            next(r for r in runs if r["run_id"] == "scratch-dir")["has_gates"]
        )

    def test_reads_phases_cuts_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")
            state = read_run(root, "run-a", now=time.time())
        self.assertTrue(state["found"])
        done = [p for p in state["phases"] if p["finished_at"]]
        # ゲート6件 + 生成物から復元した映像生成の1件
        self.assertEqual(len(done), 7)
        self.assertIn(
            "image_video_production", {p["id"] for p in done}
        )
        self.assertAlmostEqual(state["cost"]["spent_usd"], 0.6)
        cut = state["cuts"][0]
        self.assertEqual(cut["cut_id"], 1)
        self.assertEqual(cut["verdict"], "pass")
        self.assertTrue(cut["source_url"].startswith("/media/run-a/"))
        self.assertTrue(cut["clip_url"].endswith("attempt_01.mp4"))

    def test_counts_reruns(self) -> None:
        """差し戻して2回実行した工程を「2回」と数える。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")
            state = read_run(root, "run-a", now=time.time())
        creative = next(p for p in state["phases"] if p["id"] == "creative_director")
        self.assertEqual(creative["runs"], 2)

    def test_handles_a_run_in_progress(self) -> None:
        """途中までしかゲートが無くても壊れず、未実行はpendingで返す。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-partial", partial_run=True)
            state = read_run(root, "run-partial", now=time.time())
        self.assertTrue(state["found"])
        self.assertEqual(state["cuts"], [])
        self.assertEqual(state["cost"]["spent_usd"], 0.0)
        pending = [p for p in state["phases"] if p["status"] == "pending"]
        self.assertTrue(pending)

    def test_skips_cut_dirs_that_hold_only_a_decision(self) -> None:
        """`cut_00/decision.json` だけのディレクトリを「Cut 0」として出さない。

        実runで実際に作られる副産物で、表示できる情報が何も無い。
        一方、生成待ちのカットは request.json を持つので消えてはいけない。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            stray = run / "cuts" / "cut_00"
            stray.mkdir(parents=True)
            (stray / "decision.json").write_text("{}", encoding="utf-8")
            pending = run / "cuts" / "cut_02"
            pending.mkdir(parents=True)
            (pending / "request.json").write_text(
                json.dumps({"cut_id": 2, "model": "gen4.5"}), encoding="utf-8")
            cuts = {c["cut_id"] for c in
                    read_run(root, "run-a", now=time.time())["cuts"]}
        self.assertNotIn(0, cuts)
        self.assertEqual(cuts, {1, 2})     # 生成待ちのCut 2は残る

    def test_detects_generation_in_progress(self) -> None:
        """映像生成はゲートを書かない（policy: never）。

        request.json だけが存在し mp4 も error も無い試行を「生成中」と
        判定できないと、数分間なにも動いていないように見えてしまう。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            cut = run / "cuts" / "cut_02"
            cut.mkdir(parents=True)
            (cut / "attempt_01_request.json").write_text(
                json.dumps({"cut_id": 2}), encoding="utf-8")
            (cut / "request.json").write_text(
                json.dumps({"cut_id": 2, "model": "gen4.5"}), encoding="utf-8")
            state = read_run(root, "run-a", now=time.time())
        activity = state["activity"]
        self.assertEqual(activity["state"], "generating")
        self.assertEqual(activity["cut_id"], 2)
        self.assertEqual(activity["attempt"], 1)
        production = next(
            p for p in state["phases"] if p["id"] == "image_video_production"
        )
        self.assertEqual(production["state"], "active")

    def test_finished_generation_is_not_reported_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            cut = run / "cuts" / "cut_01"
            (cut / "attempt_01_request.json").write_text("{}", encoding="utf-8")
            # mp4 は _build_run が既に置いている
            state = read_run(root, "run-a", now=time.time())
        self.assertNotEqual(state["activity"]["state"], "generating")

    def test_failed_generation_is_not_reported_as_running(self) -> None:
        """失敗した試行を延々「生成中」と表示し続けないこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            cut = run / "cuts" / "cut_03"
            cut.mkdir(parents=True)
            (cut / "request.json").write_text(
                json.dumps({"cut_id": 3}), encoding="utf-8")
            (cut / "attempt_01_request.json").write_text("{}", encoding="utf-8")
            (cut / "attempt_01_error.json").write_text(
                json.dumps({"error": "RunwayError"}), encoding="utf-8")
            state = read_run(root, "run-a", now=time.time())
        self.assertNotEqual(state["activity"]["state"], "generating")

    def test_abandoned_attempt_is_not_reported_as_generating(self) -> None:
        """差し戻しで破棄された試行を「生成中」と表示し続けないこと。

        `[s]` などで差し戻すと、attempt_01 の request だけが残り、
        mp4 も error も作られないまま attempt_02 が成功する。
        後続の書き込みに追い越された要求は、生成中ではない。
        """
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            cut = run / "cuts" / "cut_02"
            cut.mkdir(parents=True)
            base = time.time() - 600
            # 破棄された1回目（mp4もerrorも無い）
            (cut / "attempt_01_request.json").write_text("{}", encoding="utf-8")
            os.utime(cut / "attempt_01_request.json", (base, base))
            # 成功した2回目（1回目より後に書かれている）
            (cut / "attempt_02_request.json").write_text("{}", encoding="utf-8")
            (cut / "attempt_02.mp4").write_bytes(b"mp4")
            for name in ("attempt_02_request.json", "attempt_02.mp4"):
                os.utime(cut / name, (base + 120, base + 120))
            state = read_run(root, "run-a", now=time.time())
        self.assertNotEqual(state["activity"]["state"], "generating")

    def test_completed_run_does_not_report_a_growing_elapsed(self) -> None:
        """完了した run で「経過」が増え続けないこと。

        止まった run に「最後の更新からの経過」を出し続けると、数字が
        際限なく伸びて意味を失う。完了は別状態にし、総所要と最終更新時刻を返す。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            (run / "final").mkdir()
            (run / "final" / "final_video.mp4").write_bytes(b"mp4")
            state = read_run(root, "run-a", now=time.time() + 86400)
        activity = state["activity"]
        self.assertEqual(activity["state"], "completed")
        self.assertIsNone(activity["elapsed_seconds"])
        self.assertIsNotNone(activity["total_seconds"])
        self.assertIsNotNone(activity["last_update_at"])

    def test_stopped_run_reports_a_timestamp_instead_of_elapsed(self) -> None:
        """中断した run も同様に、増え続ける経過を出さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")
            state = read_run(root, "run-a", now=time.time() + 86400)
        activity = state["activity"]
        self.assertEqual(activity["state"], "idle")
        self.assertIsNone(activity["elapsed_seconds"])
        self.assertIsNotNone(activity["last_update_at"])

    def test_running_run_still_reports_elapsed(self) -> None:
        """動いている間は経過を出す（これは意味のある数字）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")
            state = read_run(root, "run-a", now=time.time())
        activity = state["activity"]
        self.assertEqual(activity["state"], "waiting")
        self.assertIsNotNone(activity["elapsed_seconds"])

    def test_phase_output_is_exposed_for_the_ui(self) -> None:
        """ボタンを押して中身を見るための data が API に載ること。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            (run / "gates" / "director_attempt_01.json").write_text(
                json.dumps({
                    "phase": "director", "status": "success",
                    "summary": "2カットを設計",
                    "data": {
                        "shots": [{"id": 1, "camera_motion": "slow pan",
                                   "positive_prompt": "夜景"}],
                        "continuity_checks": ["色調"],
                        "technical_parameters_status": "n/a",
                    },
                }, ensure_ascii=False), encoding="utf-8")
            state = read_run(root, "run-a", now=time.time())
        director = next(p for p in state["phases"] if p["id"] == "director")
        self.assertEqual(
            set(director["output"]), {"shots", "continuity_checks"}
        )
        self.assertEqual(
            director["output"]["shots"][0]["camera_motion"], "slow pan"
        )

    def test_production_phase_is_reconstructed_from_artifacts(self) -> None:
        """映像生成は承認ゲートを持たない（policy: never）。

        ゲートだけを見ると「未実行」に見えるが、実際には最も時間と費用が
        かかる工程なので、生成物から実績を復元して表示する。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")     # cut_01/attempt_01.mp4 を含む
            state = read_run(root, "run-a", now=time.time())
        production = next(
            p for p in state["phases"] if p["id"] == "image_video_production"
        )
        self.assertEqual(production["status"], "success")
        self.assertEqual(production["runs"], 1)
        self.assertIsNotNone(production["finished_at"])
        self.assertIn("1カットを生成", production["summary"])
        self.assertTrue(production["output"]["generated_cuts"])

    def test_production_phase_stays_empty_when_nothing_was_generated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-partial", partial_run=True)
            state = read_run(root, "run-partial", now=time.time())
        production = next(
            p for p in state["phases"] if p["id"] == "image_video_production"
        )
        self.assertEqual(production["state"], "pending")
        self.assertIsNone(production["output"])

    def test_long_lists_are_truncated(self) -> None:
        """素材候補などが数十件あってもポーリングを重くしない。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root, "run-a")
            (run / "gates" / "asset_curator_attempt_01.json").write_text(
                json.dumps({
                    "phase": "asset_curator", "status": "success",
                    "data": {"asset_assignments": [
                        {"cut_id": i} for i in range(40)
                    ]},
                }), encoding="utf-8")
            state = read_run(root, "run-a", now=time.time())
        items = next(
            p for p in state["phases"] if p["id"] == "asset_curator"
        )["output"]["asset_assignments"]
        self.assertLess(len(items), 40)
        self.assertIn("_truncated", items[-1])

    def test_missing_run_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = read_run(Path(tmp), "run-nope", now=time.time())
        self.assertFalse(state["found"])

    def test_reader_writes_nothing(self) -> None:
        """観測が副作用を持たないこと（本番に影響しない根拠）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_run(root, "run-a")
            before = {
                str(p): (p.stat().st_mtime, p.stat().st_size)
                for p in root.rglob("*") if p.is_file()
            }
            read_run(root, "run-a", now=time.time())
            list_runs(root)
            after = {
                str(p): (p.stat().st_mtime, p.stat().st_size)
                for p in root.rglob("*") if p.is_file()
            }
        self.assertEqual(before, after)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        _build_run(cls.root, "run-a")
        handler = partial(MonitorHandler, runs_root=cls.root)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, path: str):
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.status, response.read()

    def test_serves_the_ui(self) -> None:
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"AGEWEC", body)

    def test_state_defaults_to_the_newest_run(self) -> None:
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["run_id"], "run-a")

    def test_serves_media_inside_the_run(self) -> None:
        status, body = self._get("/media/run-a/cuts/cut_01/attempt_01.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"mp4")

    def test_refuses_paths_outside_the_runs_root(self) -> None:
        """監視ツールがローカルの任意ファイルを配信しないこと。"""
        for attack in (
            "/media/../../../../etc/passwd",
            "/media/run-a/../../../../etc/passwd",
            "/media/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        ):
            with self.subTest(path=attack):
                try:
                    status, _ = self._get(attack)
                except Exception as exc:  # HTTPError も拒否として扱う
                    self.assertIn("403", str(exc) + repr(exc))
                    continue
                self.assertIn(status, (403, 404))

    def test_serves_a_downscaled_asset_photo(self) -> None:
        """素材は run 配下ではないので、専用の /asset/ で縮小して返す。"""
        from PIL import Image
        with tempfile.TemporaryDirectory() as assets:
            assets_root = Path(assets)
            big = assets_root / "asset-001_sample.jpg"
            Image.new("RGB", (4000, 3000), (20, 40, 80)).save(big, quality=90)
            handler = partial(
                MonitorHandler, runs_root=self.root, assets_root=assets_root)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}"
                with urlopen(f"{url}/asset/asset-001_sample.jpg") as response:
                    self.assertEqual(response.status, 200)
                    body = response.read()
                # 原本より小さくなっていること
                self.assertLess(len(body), big.stat().st_size)
                # ディレクトリを含む要求は拒否する
                for attack in ("/asset/../../etc/passwd", "/asset/sub%2Fdir.jpg"):
                    with self.subTest(path=attack):
                        try:
                            with urlopen(url + attack) as r:
                                self.assertIn(r.status, (403, 404))
                        except Exception as exc:
                            self.assertTrue(
                                "403" in str(exc) or "404" in str(exc), str(exc)
                            )
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_asset_endpoint_is_disabled_without_assets_root(self) -> None:
        status_seen = None
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/asset/x.jpg") as r:
                status_seen = r.status
        except Exception as exc:
            status_seen = 404 if "404" in str(exc) else None
        self.assertEqual(status_seen, 404)

    def test_no_write_methods(self) -> None:
        """POST等を実装していない＝ブラウザから状態を変えられない。"""
        self.assertFalse(hasattr(MonitorHandler, "do_POST"))
        self.assertFalse(hasattr(MonitorHandler, "do_PUT"))
        self.assertFalse(hasattr(MonitorHandler, "do_DELETE"))


if __name__ == "__main__":
    unittest.main()
