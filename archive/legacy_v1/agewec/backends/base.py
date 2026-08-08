"""[LEGACY v1] 生成バックエンドの共通インターフェース。

local / cloud を同じシグネチャで扱い、config.backend で差し替える。
実装段階で local.py に ComfyUI/diffusers、cloud.py に Kling/Veo/Runway を入れる。
"""
from __future__ import annotations

from typing import Protocol


class ImageBackend(Protocol):
    def generate_image(self, prompt: str, out_path: str) -> str: ...


class VideoBackend(Protocol):
    def image_to_video(self, image_path: str, motion_prompt: str,
                       seconds: float, out_path: str) -> str: ...


class AudioBackend(Protocol):
    def tts(self, text: str, out_path: str) -> str: ...
    def bgm(self, prompt: str, out_path: str) -> str: ...
