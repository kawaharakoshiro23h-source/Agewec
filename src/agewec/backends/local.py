"""ローカルバックエンド（スタブ）。

実装段階でここを埋める:
- generate_image: ComfyUI(HTTP) or diffusers で FLUX を叩く
- image_to_video: ComfyUI で Wan2.1 1.3B / LTX-Video (GGUF)
- tts: VOICEVOX エンジン(HTTP)
- bgm: ACE-Step

いまはパスを返すだけのモック。
"""
from __future__ import annotations

from pathlib import Path


def _touch(out_path: str, tag: str) -> str:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"MOCK {tag}\n", encoding="utf-8")
    return str(p)


class LocalImageBackend:
    def generate_image(self, prompt: str, out_path: str) -> str:
        return _touch(out_path, f"image::{prompt[:40]}")


class LocalVideoBackend:
    def image_to_video(self, image_path: str, motion_prompt: str,
                       seconds: float, out_path: str) -> str:
        return _touch(out_path, f"video::{seconds}s::{motion_prompt[:30]}")


class LocalAudioBackend:
    def tts(self, text: str, out_path: str) -> str:
        return _touch(out_path, f"tts::{text[:30]}")

    def bgm(self, prompt: str, out_path: str) -> str:
        return _touch(out_path, f"bgm::{prompt[:30]}")
