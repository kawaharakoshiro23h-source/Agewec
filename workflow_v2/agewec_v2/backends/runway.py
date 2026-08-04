"""Runway Dev API を使うクラウド動画生成 Adapter。

【本番経路: 現役】`production.backend: runway` のときに使われる。

Runway Dev API は単一の統合で複数モデルを扱えるため、Adapter は1つで済む。
モデルは設定で切り替える（比較のたびにコードを触らない）。

    production:
      backend: runway
      model: veo3.1_fast     # gen4.5 / seedance2 / hailuo3 に変えるだけ

処理の流れ（他のAdapterと同じ `generate(VideoRequest)` を実装する）:

    画像アップロード → 生成タスク作成 → タスクをポーリング → MP4を保存

尺・解像度・単価・音声有無は **モデル単位** に設定へ持つ（config の runway.models）。
可能なモデルでは音声を生成させない（`request_audio: false`）。最終的な音は
Phase 08 で一本のBGM/ナレーションとして付ける方針のため。
"""
from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import Capabilities, VideoRequest, VideoResult

# タスクの終了状態（Runwayの表記ゆれに備えて広めに持つ）
_SUCCESS = {"SUCCEEDED", "SUCCESS", "COMPLETED"}
_FAILURE = {"FAILED", "ERROR", "CANCELLED", "CANCELED"}

# 生成方式ごとのエンドポイント。Runwayは方式でURLが分かれる。
_GENERATION_ENDPOINTS = {
    "image_to_video": "v1/image_to_video",
    "text_to_video": "v1/text_to_video",
}


class RunwayError(RuntimeError):
    """Runway API 呼び出しの失敗。"""


class RunwayBackend:
    """Runway Dev API 経由で image-to-video を実行する。"""

    provider = "runway"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        models: dict[str, Any],
        output_path_for: Callable[[int, int], Any],
        base_url: str = "https://api.dev.runwayml.com",
        api_version: str = "2024-11-06",
        ratio: str = "1280:720",
        poll_interval: float = 5.0,
        timeout: float = 900.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise RunwayError(
                "RUNWAY_API_KEY が未設定です（.env に設定してください）"
            )
        self.api_key = api_key
        self.model = model
        self.models = models or {}
        self._output_path_for = output_path_for
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.ratio = ratio
        self.poll_interval = float(poll_interval)
        self.timeout = float(timeout)
        self._client = client or httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------ 能力定義
    def capabilities(self, model: str | None = None) -> Capabilities:
        name = model or self.model
        spec = self.models.get(name)
        if spec is None:
            raise RunwayError(
                f"未知のモデル: {name}（config の runway.models に定義が必要）"
            )
        return Capabilities(
            model=name,
            allowed_seconds=tuple(
                float(value) for value in spec.get("allowed_seconds", ())
            ),
            max_seconds=float(spec.get("max_seconds", 0.0)),
            resolutions=tuple(spec.get("resolutions", ())),
            supports_seed=bool(spec.get("supports_seed", True)),
            supports_negative_prompt=bool(
                spec.get("supports_negative_prompt", True)
            ),
            has_native_audio=bool(spec.get("has_native_audio", False)),
            cost_per_second_usd=float(spec.get("cost_per_second_usd", 0.0)),
            minimum_cost_usd=float(spec.get("minimum_cost_usd", 0.0)),
        )

    # ------------------------------------------------------------- HTTP層
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Runway-Version": self.api_version,
        }

    def _upload_image(self, image_path: Path) -> str:
        """公式の ephemeral upload 手順で画像をアップロードする。

        `/v1/uploads` はファイル本体を直接受け取らない。最初に JSON で
        uploadUrl / fields / runwayUri を発行し、次に署名付き uploadUrl へ
        fields とファイル本体を multipart POST する。生成APIへ渡す値は
        uploadUrl ではなく runwayUri。
        """
        mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        prepare = self._client.post(
            f"{self.base_url}/v1/uploads",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"filename": image_path.name, "type": "ephemeral"},
        )
        if prepare.status_code >= 400:
            raise RunwayError(
                f"アップロード準備失敗 HTTP {prepare.status_code}: "
                f"{prepare.text[:200]}"
            )
        body = prepare.json()
        upload_url = body.get("uploadUrl")
        fields = body.get("fields")
        runway_uri = body.get("runwayUri")
        if not upload_url or not isinstance(fields, dict) or not runway_uri:
            raise RunwayError(
                "アップロード準備応答に uploadUrl / fields / runwayUri "
                f"がありません: {body}"
            )

        with image_path.open("rb") as stream:
            upload = self._client.post(
                str(upload_url),
                data=fields,
                files={"file": (image_path.name, stream, mime)},
                timeout=self.timeout,
            )
        if upload.status_code >= 400:
            raise RunwayError(
                f"画像アップロード失敗 HTTP {upload.status_code}: "
                f"{upload.text[:200]}"
            )
        return str(runway_uri)

    def _create_task(self, payload: dict[str, Any], endpoint: str) -> str:
        response = self._client.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RunwayError(
                f"生成依頼失敗 HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        body = response.json()
        task_id = body.get("id") or body.get("taskId")
        if not task_id:
            raise RunwayError(f"応答にタスクIDがありません: {body}")
        return str(task_id)

    def _wait(self, task_id: str) -> dict[str, Any]:
        """完了までポーリングする。タイムアウトと失敗は例外にする。"""
        deadline = time.monotonic() + self.timeout
        while True:
            response = self._client.get(
                f"{self.base_url}/v1/tasks/{task_id}",
                headers=self._headers(),
            )
            if response.status_code >= 400:
                raise RunwayError(
                    f"タスク照会失敗 HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            body = response.json()
            status = str(body.get("status", "")).upper()
            if status in _SUCCESS:
                return body
            if status in _FAILURE:
                raise RunwayError(
                    f"生成失敗 status={status}: "
                    f"{body.get('failure') or body.get('error') or ''}"
                )
            if time.monotonic() > deadline:
                raise RunwayError(
                    f"生成がタイムアウトしました（{self.timeout}秒） task={task_id}"
                )
            time.sleep(self.poll_interval)

    @staticmethod
    def _output_url(body: dict[str, Any]) -> str:
        output = body.get("output")
        if isinstance(output, list) and output:
            first = output[0]
            return first if isinstance(first, str) else str(
                first.get("url", "")
            )
        if isinstance(output, dict):
            return str(output.get("url", ""))
        if isinstance(output, str):
            return output
        raise RunwayError(f"応答に出力URLがありません: {body}")

    def _download(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, timeout=self.timeout) as response:
            if response.status_code >= 400:
                raise RunwayError(f"ダウンロード失敗 HTTP {response.status_code}")
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return destination

    # -------------------------------------------------------------- 生成
    def generate(self, request: VideoRequest) -> VideoResult:
        started = time.monotonic()
        caps = self.capabilities(
            str(request.extra.get("model") or "") or None
        )
        # 許容尺へ切り上げる（短くはしない。上限超過はここで例外）
        duration = caps.resolve_seconds(float(request.seconds))
        spec = self.models.get(caps.model, {})

        # 生成方式はカット単位で Director が決め、ProductionRequest 経由で
        # ここへ届く。画像の有無から推測しない（素材選定の失敗を
        # text_to_video として静かに握り潰さないため）。
        mode = str(request.extra.get("generation_mode") or "image_to_video")
        if mode not in _GENERATION_ENDPOINTS:
            raise RunwayError(
                f"未知の generation_mode: {mode}"
                f"（有効: {', '.join(sorted(_GENERATION_ENDPOINTS))}）"
            )
        supported_modes = tuple(
            str(value)
            for value in spec.get(
                "generation_modes", tuple(_GENERATION_ENDPOINTS)
            )
        )
        if mode not in supported_modes:
            raise RunwayError(
                f"Runway {caps.model} は現在 {mode} に未対応です"
                f"（対応: {', '.join(supported_modes)}）"
            )
        endpoint = _GENERATION_ENDPOINTS[mode]

        payload: dict[str, Any] = {
            "model": caps.model,
            "promptText": request.positive_prompt,
            "duration": int(round(duration)),
        }
        resolution = str(spec.get("resolution") or "")
        if resolution:
            # Hailuo 3.0など、幅:高さではなく品質トークンを受け取るモデル。
            payload["resolution"] = resolution
        else:
            payload["ratio"] = str(spec.get("ratio") or self.ratio)
        if mode == "image_to_video":
            if not request.image_path:
                raise RunwayError(
                    f"cut {request.cut_id}: image_to_video に画像がありません"
                )
            uploaded = self._upload_image(Path(request.image_path))
            if str(spec.get("prompt_image_format") or "uri") == "keyframes":
                # Hailuo 3.0のRunway契約は開始・終了フレームの配列形式。
                # 現在のパイプラインは開始フレーム1枚だけを渡す。
                payload["promptImage"] = [
                    {"uri": uploaded, "position": "first"}
                ]
            else:
                payload["promptImage"] = uploaded
        elif request.image_path:
            raise RunwayError(
                f"cut {request.cut_id}: text_to_video に画像は渡せません"
                f"（{request.image_path}）"
            )
        if caps.supports_seed and request.seed is not None:
            payload["seed"] = int(request.seed)
        if caps.supports_negative_prompt and request.negative_prompt:
            payload["negativePrompt"] = request.negative_prompt
        if "request_audio" in spec:
            # 可能なモデルでは音声を生成させない（音は最後に一本だけ付ける）
            payload["audio"] = bool(spec["request_audio"])

        task_id = self._create_task(payload, endpoint)
        body = self._wait(task_id)
        destination = Path(
            str(self._output_path_for(request.cut_id, request.attempt))
        )
        self._download(self._output_url(body), destination)

        return VideoResult(
            output_path=str(destination),
            provider=self.provider,
            model=caps.model,
            requested_seconds=float(request.seconds),
            billed_seconds=duration,
            actual_seconds=duration,
            job_id=task_id,
            elapsed_seconds=round(time.monotonic() - started, 3),
            # 秒単価だけでなく、モデル固有の最低課金も反映する。
            cost_usd=round(caps.estimate_cost(float(request.seconds)), 6),
            has_native_audio=(
                caps.has_native_audio and payload.get("audio", True) is not False
            ),
            settings={
                **payload,
                "generation_mode": mode,
                "endpoint": endpoint,
                # アップロード先URLは毎回変わり証跡として意味がないため伏せる
                **(
                    {"promptImage": "<uploaded>"}
                    if "promptImage" in payload
                    else {}
                ),
            },
        )


def build_runway_backend(
    config: dict[str, Any],
    *,
    model: str,
    output_path_for: Callable[[int, int], Any],
    client: httpx.Client | None = None,
) -> RunwayBackend:
    """config（runway セクション）から Adapter を組み立てる。"""
    key_env = str(config.get("api_key_env", "RUNWAY_API_KEY"))
    return RunwayBackend(
        api_key=os.environ.get(key_env, ""),
        model=model,
        models=config.get("models", {}),
        output_path_for=output_path_for,
        base_url=str(config.get("base_url", "https://api.dev.runwayml.com")),
        api_version=str(config.get("api_version", "2024-11-06")),
        ratio=str(config.get("ratio", "1280:720")),
        poll_interval=float(config.get("poll_interval_seconds", 5)),
        timeout=float(config.get("timeout_seconds", 900)),
        client=client,
    )
