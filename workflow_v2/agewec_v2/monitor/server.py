"""監視用のローカルHTTPサーバ（読み取り専用）。

標準ライブラリだけで動く。ビルド手順も追加依存もない。

エンドポイント:
    GET /                     監視画面（HTML）
    GET /api/runs             run の一覧（新しい順）
    GET /api/state?run=<id>   その run の現在状態
    GET /media/<run>/<path>   画像・動画の配信（run配下に限定）
    GET /asset/<filename>     素材写真のサムネイル（assets_dl配下に限定）

将来ブラウザから操作したくなったら `POST /api/decision` を足す。その際も
既存の GET は変えずに済むよう、状態取得と表示を JSON で分離してある。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import tempfile
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .reader import list_runs, read_run

UI_PATH = Path(__file__).with_name("ui.html")
# 素材の原本は最大20MB規模。毎回そのまま返すとポーリングで詰まるため、
# 縮小したものを一時ディレクトリに置いて使い回す（原本には触れない）。
THUMB_MAX_EDGE = 900
_THUMB_CACHE = Path(tempfile.gettempdir()) / "agewec-monitor-thumbs"


class MonitorHandler(BaseHTTPRequestHandler):
    """runs_root と assets_root の配下だけを見る。書き込みは実装しない。"""

    server_version = "AgewecMonitor/1.0"

    def __init__(
        self,
        *args,
        runs_root: Path,
        assets_root: Path | None = None,
        **kwargs,
    ) -> None:
        self.runs_root = runs_root
        self.assets_root = assets_root
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------ 送信補助
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 監視画面は常に最新を見たいのでキャッシュさせない
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def log_message(self, *args) -> None:  # noqa: D102 - 端末を汚さない
        return

    # ---------------------------------------------------------- ルーティング
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if route == "/api/runs":
            self._json({"runs": list_runs(self.runs_root)})
            return
        if route == "/api/state":
            query = parse_qs(parsed.query)
            run_id = (query.get("run") or [""])[0]
            if not run_id:
                runs = list_runs(self.runs_root)
                if not runs:
                    self._json({"found": False, "run_id": None})
                    return
                run_id = runs[0]["run_id"]      # 既定は最新のrun
            self._json(read_run(self.runs_root, run_id, now=time.time()))
            return
        if route.startswith("/media/"):
            self._serve_media(route[len("/media/"):])
            return
        if route.startswith("/asset/"):
            self._serve_asset(route[len("/asset/"):])
            return
        self._json({"error": "not found"}, code=404)

    do_HEAD = do_GET

    # -------------------------------------------------------------- メディア
    def _serve_media(self, relative: str) -> None:
        """runs_root 配下のファイルだけを返す。

        `..` などで外へ出ようとする要求は、解決後のパスが runs_root の下に
        あるかで弾く（監視ツールがローカルの任意ファイルを配信しないため）。
        """
        try:
            target = (self.runs_root / unquote(relative)).resolve()
            target.relative_to(self.runs_root.resolve())
        except (ValueError, OSError):
            self._json({"error": "forbidden"}, code=403)
            return
        if not target.is_file():
            self._json({"error": "not found"}, code=404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or (
            "application/octet-stream"
        )
        self._send(200, target.read_bytes(), content_type)

    # -------------------------------------------------------------- 素材写真
    def _serve_asset(self, relative: str) -> None:
        """素材写真を縮小して返す。

        素材は run 配下ではなく `assets_dl/` にあるため `/media/` では
        出せない。ここでも「ファイル名だけを受け取り、assets_root 直下に
        解決できたものだけ」を返して、任意ファイルの配信を防ぐ。
        """
        if self.assets_root is None or not self.assets_root.is_dir():
            self._json({"error": "assets not configured"}, code=404)
            return
        name = Path(unquote(relative)).name      # ディレクトリ部分は捨てる
        if not name or name != unquote(relative):
            self._json({"error": "forbidden"}, code=403)
            return
        try:
            source = (self.assets_root / name).resolve()
            source.relative_to(self.assets_root.resolve())
        except (ValueError, OSError):
            self._json({"error": "forbidden"}, code=403)
            return
        if not source.is_file():
            self._json({"error": "not found"}, code=404)
            return
        self._send(200, self._thumbnail(source), "image/jpeg")

    def _thumbnail(self, source: Path) -> bytes:
        """縮小版を一時ディレクトリにキャッシュして返す。

        キーにファイル名と更新時刻を含めるので、素材が差し替わっても
        古い画像を返し続けることはない。
        """
        stat = source.stat()
        key = hashlib.sha256(
            f"{source}:{stat.st_mtime_ns}:{THUMB_MAX_EDGE}".encode()
        ).hexdigest()[:24]
        cached = _THUMB_CACHE / f"{key}.jpg"
        if not cached.is_file():
            _THUMB_CACHE.mkdir(parents=True, exist_ok=True)
            try:
                # 既存の縮小処理を使う（原本は読み取るだけ）
                from ..media_tools import downscale_image

                downscale_image(
                    str(source), str(cached), max_edge=THUMB_MAX_EDGE
                )
            except Exception:  # noqa: BLE001 - 縮小できなければ原本を返す
                return source.read_bytes()
        return cached.read_bytes()


def serve(
    runs_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    assets_root: Path | None = None,
) -> None:
    handler = partial(
        MonitorHandler, runs_root=runs_root, assets_root=assets_root
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"監視画面: http://{host}:{httpd.server_address[1]}/")
    print(f"監視対象: {runs_root}")
    if assets_root and assets_root.is_dir():
        print(f"素材写真: {assets_root}")
    print("停止するには Ctrl-C（パイプラインには影響しません）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
    finally:
        httpd.server_close()
