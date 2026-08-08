"""監視サーバの起動口。

    PYTHONPATH=workflow_v2 .venv/bin/python -m agewec_v2.monitor

パイプラインとは別プロセスで動く。実行中に起動しても、途中で止めても、
本番の挙動には影響しない（読み取りしかしないため）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..paths import runtime_paths
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        description="実行中のワークフローをブラウザから監視する（読み取り専用）",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=runtime_paths().runs_root,
        help="run が並ぶディレクトリ（既定: runtime/runs）",
    )
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=runtime_paths().assets_root,
        help="素材写真の置き場（既定: assets_dl）。選定素材のサムネイル用",
    )
    parser.add_argument("--host", default="127.0.0.1")
    # 3000番は他ツールと衝突しやすいので既定から外す
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    runs_root = args.runs_root.resolve()
    if not runs_root.is_dir():
        parser.error(f"ディレクトリがありません: {runs_root}")
    assets_root = args.assets_root.resolve()
    serve(
        runs_root,
        host=args.host,
        port=args.port,
        assets_root=assets_root if assets_root.is_dir() else None,
    )


if __name__ == "__main__":
    main()
