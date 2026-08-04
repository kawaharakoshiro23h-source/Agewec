"""既存の Comfy / mock 生成を、共通契約で包んだ Adapter。

【重要】ここでは **挙動を一切変えない**（純粋な委譲）。
秒→フレーム変換などの責務移動は、クラウドAdapter追加後・テストが安定してから
別途行う。ラップと責務移動を同時にやると尺計算を壊すため。

すべての Adapter は **同じシグネチャ** を持つ:

    generate(request: VideoRequest) -> VideoResult

state や出力先など、バックエンド固有の依存はコンストラクタで束縛する。
これによりバックエンドを追加しても呼び出し側に分岐が増えない。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .base import Capabilities, VideoRequest, VideoResult


class MockBackend:
    """テスト用。実際のモデルは呼ばず、指定尺の単色動画を作る。"""

    provider = "mock"

    def __init__(
        self,
        generate_mock_video: Callable[..., dict[str, Any]],
        output_path_for: Callable[[int, int], Any],
    ) -> None:
        self._generate = generate_mock_video
        self._output_path_for = output_path_for

    def capabilities(self, model: str | None = None) -> Capabilities:
        return Capabilities(
            model="mock",
            allowed_seconds=(),        # 任意長
            max_seconds=0.0,
            supports_seed=True,
            has_native_audio=False,
            cost_per_second_usd=0.0,   # 無料
        )

    def generate(self, request: VideoRequest) -> VideoResult:
        started = time.monotonic()
        output_path = str(
            self._output_path_for(request.cut_id, request.attempt)
        )
        seconds = float(request.seconds)
        generation = self._generate(
            output_path,
            duration_seconds=seconds,
            width=request.width,
            height=request.height,
            fps=request.fps,
            cut_id=request.cut_id,
        )
        return VideoResult(
            output_path=output_path,
            provider=self.provider,
            model="mock",
            requested_seconds=seconds,
            billed_seconds=seconds,
            actual_seconds=seconds,
            elapsed_seconds=round(time.monotonic() - started, 3),
            cost_usd=0.0,
            settings=dict(generation) if isinstance(generation, dict) else {},
        )


class ComfyBackend:
    """ローカル ComfyUI(LTX)。無料だが遅い。

    既存の `_generate_comfy(state, request_dict)` をそのまま呼ぶ薄いラッパ。
    フレーム数の算出は従来どおり support_video_creator 側にある。
    """

    provider = "comfy"

    def __init__(
        self,
        generate_comfy: Callable[..., dict[str, Any]],
        state: Any,
    ) -> None:
        self._generate = generate_comfy
        self._state = state

    def capabilities(self, model: str | None = None) -> Capabilities:
        return Capabilities(
            model=model or "ltx-video-2b",
            allowed_seconds=(),        # 8n+1 の制約は既存実装が担当
            max_seconds=10.7,          # 257フレーム / 24fps
            supports_seed=True,
            supports_negative_prompt=True,
            has_native_audio=False,
            cost_per_second_usd=0.0,   # ローカル実行なので無料
        )

    def generate(self, request: VideoRequest) -> VideoResult:
        started = time.monotonic()
        # 既存実装は生の ProductionRequest 辞書を受け取るため、そのまま渡す
        raw = dict(request.extra.get("raw") or {})
        generation = self._generate(self._state, raw)
        output_path = str(generation["output_path"])
        seconds = float(request.seconds)
        return VideoResult(
            output_path=output_path,
            provider=self.provider,
            model=str(
                (generation.get("settings") or {}).get("model")
                or "ltx-video-2b"
            ),
            requested_seconds=float(
                raw.get("requested_seconds") or seconds
            ),
            billed_seconds=seconds,
            actual_seconds=seconds,
            job_id=str(generation.get("prompt_id") or "") or None,
            elapsed_seconds=float(
                generation.get("elapsed_seconds")
                or round(time.monotonic() - started, 3)
            ),
            cost_usd=0.0,
            settings=dict(generation.get("settings") or {}),
        )


def to_video_request(
    raw: dict[str, Any], *, attempt: int = 1
) -> VideoRequest:
    """既存の ProductionRequest 辞書を共通契約へ変換する。

    元の辞書は `extra["raw"]` に保持し、既存実装へそのまま渡せるようにする。
    """
    return VideoRequest(
        cut_id=int(raw.get("cut_id", 0)),
        image_path=str(raw.get("image_path", "")),
        positive_prompt=str(raw.get("positive_prompt", "")),
        negative_prompt=str(raw.get("negative_prompt", "")),
        seconds=float(raw.get("actual_seconds") or 0.0),
        width=int(raw.get("width", 576)),
        height=int(raw.get("height", 384)),
        fps=int(raw.get("fps", 24)),
        seed=raw.get("seed"),
        attempt=attempt,
        extra={
            "raw": raw,
            "model": raw.get("model"),
            # 生成方式は Director がカット単位で決めた値をそのまま運ぶ。
            # 未指定は従来どおり image_to_video（既存runとの互換）。
            "generation_mode": (
                raw.get("generation_mode") or "image_to_video"
            ),
        },
    )


def resolve_backend(
    name: str,
    *,
    state: Any = None,
    generate_comfy: Callable[..., dict[str, Any]] | None = None,
    generate_mock_video: Callable[..., dict[str, Any]] | None = None,
    output_path_for: Callable[[int, int], Any] | None = None,
    runway_config: dict[str, Any] | None = None,
    model: str | None = None,
) -> Any:
    """設定名から Adapter を返す。将来ここに runway などを足す。

    呼び出し側は返された Adapter に対して `generate(VideoRequest)` を呼ぶだけで
    よく、バックエンドごとの分岐を持たない。
    """
    if name == "mock":
        if generate_mock_video is None or output_path_for is None:
            raise ValueError(
                "mock backend requires generate_mock_video and output_path_for"
            )
        return MockBackend(generate_mock_video, output_path_for)
    if name == "comfy":
        if generate_comfy is None:
            raise ValueError("comfy backend requires generate_comfy")
        return ComfyBackend(generate_comfy, state)
    if name == "runway":
        from .runway import build_runway_backend

        if output_path_for is None:
            raise ValueError("runway backend requires output_path_for")
        return build_runway_backend(
            runway_config or {},
            model=model or "veo3.1_fast",
            output_path_for=output_path_for,
        )
    raise ValueError(f"Unsupported production backend: {name}")
