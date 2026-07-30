"""Small ComfyUI HTTP client driven by an exported API-format workflow."""
from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ComfyGenerationRequest:
    image_path: str
    positive_prompt: str
    negative_prompt: str = ""
    width: int = 576
    height: int = 384
    frames: int = 49
    steps: int = 20
    fps: int = 24
    seed: int = 1
    file_prefix: str = "agewec_v2"


class ComfyClient:
    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: Path,
        input_mapping: dict[str, dict[str, str]],
        output_dir: Path,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow_path = workflow_path
        self.input_mapping = input_mapping
        self.output_dir = output_dir
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def _workflow(self) -> dict[str, Any]:
        if not self.workflow_path.exists():
            raise FileNotFoundError(
                f"Comfy API workflow not found: {self.workflow_path}"
            )
        return json.loads(self.workflow_path.read_text(encoding="utf-8"))

    def _patch(
        self,
        workflow: dict[str, Any],
        key: str,
        value: Any,
        *,
        required: bool = False,
    ) -> None:
        mapping = self.input_mapping.get(key, {})
        node_id = str(mapping.get("node_id") or "")
        input_name = mapping.get("input")
        if not node_id or not input_name:
            if required:
                raise ValueError(f"Comfy input mapping is missing: {key}")
            return
        try:
            workflow[node_id]["inputs"][input_name] = value
        except KeyError as exc:
            raise KeyError(
                f"Invalid Comfy mapping {key}: node={node_id}, input={input_name}"
            ) from exc

    def _upload_image(self, image_path: Path) -> str:
        if not image_path.exists():
            raise FileNotFoundError(f"Input image not found: {image_path}")
        with image_path.open("rb") as image_file:
            response = httpx.post(
                f"{self.base_url}/upload/image",
                files={"image": (image_path.name, image_file)},
                data={"overwrite": "true", "type": "input"},
                timeout=120.0,
            )
        response.raise_for_status()
        uploaded = response.json()
        name = uploaded["name"]
        subfolder = uploaded.get("subfolder", "")
        return f"{subfolder}/{name}" if subfolder else name

    def _queue(self, workflow: dict[str, Any]) -> str:
        response = httpx.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
            timeout=60.0,
        )
        response.raise_for_status()
        return str(response.json()["prompt_id"])

    def _wait_for_history(self, prompt_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=30.0,
            )
            response.raise_for_status()
            history = response.json()
            if prompt_id in history:
                item = history[prompt_id]
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution failed: {status}")
                return item
            time.sleep(self.poll_interval)
        raise TimeoutError(f"ComfyUI timed out: prompt_id={prompt_id}")

    @staticmethod
    def _find_output(history: dict[str, Any]) -> dict[str, Any]:
        for node_output in history.get("outputs", {}).values():
            for key in ("videos", "gifs", "images"):
                values = node_output.get(key) or []
                if values:
                    return values[0]
        raise RuntimeError("ComfyUI history contains no downloadable output")

    def _download_output(self, output: dict[str, Any]) -> Path:
        response = httpx.get(
            f"{self.base_url}/view",
            params={
                "filename": output["filename"],
                "subfolder": output.get("subfolder", ""),
                "type": output.get("type", "output"),
            },
            timeout=180.0,
        )
        response.raise_for_status()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / Path(output["filename"]).name
        destination.write_bytes(response.content)
        return destination

    def generate(self, request: ComfyGenerationRequest) -> dict[str, Any]:
        started = time.monotonic()
        workflow = copy.deepcopy(self._workflow())
        uploaded_name = self._upload_image(Path(request.image_path))
        values = {
            "image": uploaded_name,
            "positive_prompt": request.positive_prompt,
            "negative_prompt": request.negative_prompt,
            "width": request.width,
            "height": request.height,
            "frames": request.frames,
            "steps": request.steps,
            "fps": request.fps,
            "seed": request.seed,
            "file_prefix": request.file_prefix,
        }
        for key, value in values.items():
            self._patch(
                workflow,
                key,
                value,
                required=key in {"image", "positive_prompt"},
            )

        prompt_id = self._queue(workflow)
        history = self._wait_for_history(prompt_id)
        output = self._find_output(history)
        output_path = self._download_output(output)
        return {
            "prompt_id": prompt_id,
            "output_path": str(output_path),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "settings": {
                "width": request.width,
                "height": request.height,
                "frames": request.frames,
                "steps": request.steps,
                "fps": request.fps,
                "seed": request.seed,
            },
        }
