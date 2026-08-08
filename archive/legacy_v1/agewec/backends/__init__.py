"""[LEGACY v1] config.backend に応じたバックエンド選択。"""
from __future__ import annotations

from . import local


def get_backends(backend: str = "local"):
    """(image, video, audio) のタプルを返す。

    cloud は実装段階で追加。いまは local のみ。
    """
    if backend == "local":
        return (local.LocalImageBackend(),
                local.LocalVideoBackend(),
                local.LocalAudioBackend())
    raise NotImplementedError(f"backend '{backend}' は未実装（local のみ対応）")
