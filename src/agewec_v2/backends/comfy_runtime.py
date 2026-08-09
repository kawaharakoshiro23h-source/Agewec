"""Validated ComfyUI runtime for exported API-format workflows."""
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


@dataclass(frozen=True)
class InputTarget:
    node_id: str
    input_name: str


class ComfyWorkflowError(ValueError):
    pass


class ComfyWorkflow:
    """API workflow loader, input auto-discovery, and validation."""

    REQUIRED_FIELDS = {
        "image",
        "positive_prompt",
        "width",
        "height",
        "frames",
        "steps",
        "fps",
        "seed",
        "file_prefix",
    }

    def __init__(
        self,
        workflow: dict[str, Any],
        explicit_mapping: dict[str, dict[str, str]] | None = None,
    ) -> None:
        if "nodes" in workflow and isinstance(workflow.get("nodes"), list):
            raise ComfyWorkflowError(
                "This is ComfyUI UI-format JSON, not API-format JSON. "
                "Enable Dev Mode and save/export the workflow in API format."
            )
        if not workflow or not all(
            isinstance(node, dict) and "class_type" in node
            for node in workflow.values()
        ):
            raise ComfyWorkflowError(
                "Workflow must be a ComfyUI API-format object keyed by node ID."
            )
        self.workflow = workflow
        self.mapping = self._discover_mapping()
        self._apply_explicit_mapping(explicit_mapping or {})
        missing = sorted(self.REQUIRED_FIELDS - self.mapping.keys())
        if missing:
            raise ComfyWorkflowError(
                "Could not map required Comfy inputs: " + ", ".join(missing)
            )

    @classmethod
    def from_file(
        cls,
        path: Path,
        explicit_mapping: dict[str, dict[str, str]] | None = None,
    ) -> "ComfyWorkflow":
        if not path.exists():
            raise FileNotFoundError(
                f"Comfy API workflow not found: {path}. "
                "Export the working graph as ltx_i2v_api.json."
            )
        return cls(
            json.loads(path.read_text(encoding="utf-8")),
            explicit_mapping,
        )

    def _nodes_with_input(self, input_name: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            (str(node_id), node)
            for node_id, node in self.workflow.items()
            if input_name in node.get("inputs", {})
        ]

    def _first(
        self,
        *,
        class_types: set[str] | None = None,
        input_name: str,
    ) -> InputTarget | None:
        for node_id, node in self._nodes_with_input(input_name):
            if class_types and node.get("class_type") not in class_types:
                continue
            return InputTarget(node_id, input_name)
        return None

    def _conditioning_prompt_nodes(self) -> dict[str, InputTarget]:
        result: dict[str, InputTarget] = {}
        for node_id, node in self.workflow.items():
            if node.get("class_type") not in {
                "LTXVConditioning",
                "LTXVConditioningV2",
            }:
                continue
            inputs = node.get("inputs", {})
            for key, field in (
                ("positive", "positive_prompt"),
                ("negative", "negative_prompt"),
            ):
                link = inputs.get(key)
                if isinstance(link, list) and link:
                    source_id = str(link[0])
                    source = self.workflow.get(source_id, {})
                    if (
                        source.get("class_type") == "CLIPTextEncode"
                        and "text" in source.get("inputs", {})
                    ):
                        result[field] = InputTarget(source_id, "text")
        return result

    def _prompt_nodes_by_title(self) -> dict[str, InputTarget]:
        result: dict[str, InputTarget] = {}
        unclassified = []
        for node_id, node in self.workflow.items():
            if node.get("class_type") != "CLIPTextEncode":
                continue
            if "text" not in node.get("inputs", {}):
                continue
            title = str(node.get("_meta", {}).get("title", "")).lower()
            target = InputTarget(str(node_id), "text")
            if "negative" in title:
                result["negative_prompt"] = target
            elif "positive" in title:
                result["positive_prompt"] = target
            else:
                unclassified.append(target)
        if "positive_prompt" not in result and unclassified:
            result["positive_prompt"] = unclassified[0]
        if "negative_prompt" not in result and len(unclassified) > 1:
            result["negative_prompt"] = unclassified[1]
        return result

    def _discover_mapping(self) -> dict[str, InputTarget]:
        mapping: dict[str, InputTarget] = {}
        direct = {
            "image": (
                {"LoadImage", "LoadImageOutput"},
                "image",
            ),
            "width": (
                {"LTXVImgToVideo", "LTXVImgToVideoInplace"},
                "width",
            ),
            "height": (
                {"LTXVImgToVideo", "LTXVImgToVideoInplace"},
                "height",
            ),
            "frames": (
                {"LTXVImgToVideo", "LTXVImgToVideoInplace"},
                "length",
            ),
            "steps": (
                {"LTXVScheduler", "BasicScheduler"},
                "steps",
            ),
            "fps": (
                {"CreateVideo", "VHS_VideoCombine"},
                "fps",
            ),
            "file_prefix": (
                {"SaveVideo", "VHS_VideoCombine"},
                "filename_prefix",
            ),
        }
        for field, (class_types, input_name) in direct.items():
            target = self._first(
                class_types=class_types,
                input_name=input_name,
            )
            if target:
                mapping[field] = target

        seed = self._first(
            class_types={"SamplerCustom", "RandomNoise", "KSampler"},
            input_name="noise_seed",
        ) or self._first(
            class_types={"KSampler"},
            input_name="seed",
        )
        if seed:
            mapping["seed"] = seed

        mapping.update(self._prompt_nodes_by_title())
        mapping.update(self._conditioning_prompt_nodes())
        return mapping

    def _apply_explicit_mapping(
        self,
        explicit: dict[str, dict[str, str]],
    ) -> None:
        for field, raw in explicit.items():
            node_id = str(raw.get("node_id") or "")
            input_name = str(raw.get("input") or "")
            if not node_id or not input_name:
                continue
            node = self.workflow.get(node_id)
            if not node or input_name not in node.get("inputs", {}):
                raise ComfyWorkflowError(
                    f"Invalid explicit mapping {field}: "
                    f"node={node_id}, input={input_name}"
                )
            self.mapping[field] = InputTarget(node_id, input_name)

    def class_types(self) -> set[str]:
        return {
            str(node["class_type"])
            for node in self.workflow.values()
        }

    def mapping_report(self) -> dict[str, dict[str, str]]:
        return {
            field: {
                "node_id": target.node_id,
                "input": target.input_name,
                "class_type": self.workflow[target.node_id]["class_type"],
            }
            for field, target in sorted(self.mapping.items())
        }

    def patched(self, values: dict[str, Any]) -> dict[str, Any]:
        workflow = copy.deepcopy(self.workflow)
        for field, value in values.items():
            target = self.mapping.get(field)
            if not target:
                continue
            workflow[target.node_id]["inputs"][target.input_name] = value
        return workflow


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
        self._workflow: ComfyWorkflow | None = None

    @property
    def workflow(self) -> ComfyWorkflow:
        if self._workflow is None:
            self._workflow = ComfyWorkflow.from_file(
                self.workflow_path,
                self.input_mapping,
            )
        return self._workflow

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        detail = response.text[:2000]
        raise RuntimeError(
            f"ComfyUI {operation} failed: HTTP {response.status_code}: {detail}"
        )

    def preflight(self) -> dict[str, Any]:
        stats_response = httpx.get(
            f"{self.base_url}/system_stats",
            timeout=10.0,
        )
        self._raise_for_status(stats_response, "system_stats")
        object_response = httpx.get(
            f"{self.base_url}/object_info",
            timeout=30.0,
        )
        self._raise_for_status(object_response, "object_info")
        available = set(object_response.json())
        required = self.workflow.class_types()
        missing = sorted(required - available)
        if missing:
            raise ComfyWorkflowError(
                "ComfyUI is missing workflow node classes: "
                + ", ".join(missing)
            )
        return {
            "connected": True,
            "workflow_path": str(self.workflow_path),
            "node_count": len(self.workflow.workflow),
            "mapping": self.workflow.mapping_report(),
            "required_class_count": len(required),
            "missing_class_types": [],
            "system": stats_response.json().get("system", {}),
        }

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
        self._raise_for_status(response, "upload")
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
        self._raise_for_status(response, "queue prompt")
        body = response.json()
        if body.get("node_errors"):
            raise RuntimeError(
                "ComfyUI rejected workflow nodes: "
                + json.dumps(body["node_errors"], ensure_ascii=False)
            )
        return str(body["prompt_id"])

    def _wait_for_history(self, prompt_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=30.0,
            )
            self._raise_for_status(response, "history")
            history = response.json()
            if prompt_id in history:
                item = history[prompt_id]
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(
                        "ComfyUI execution failed: "
                        + json.dumps(status, ensure_ascii=False)
                    )
                return item
            time.sleep(self.poll_interval)
        raise TimeoutError(f"ComfyUI timed out: prompt_id={prompt_id}")

    @staticmethod
    def _find_outputs(history: dict[str, Any]) -> list[dict[str, Any]]:
        outputs = []
        for node_output in history.get("outputs", {}).values():
            for key in ("videos", "gifs", "images"):
                for value in node_output.get(key) or []:
                    outputs.append({**value, "media_kind": key})
        if not outputs:
            raise RuntimeError("ComfyUI history contains no downloadable output")
        outputs.sort(key=lambda item: item["media_kind"] != "videos")
        return outputs

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
        self._raise_for_status(response, "download output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / Path(output["filename"]).name
        destination.write_bytes(response.content)
        return destination

    def generate(self, request: ComfyGenerationRequest) -> dict[str, Any]:
        started = time.monotonic()
        self.preflight()
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
        workflow = self.workflow.patched(values)
        prompt_id = self._queue(workflow)
        history = self._wait_for_history(prompt_id)
        output_descriptors = self._find_outputs(history)
        paths = [
            str(self._download_output(output))
            for output in output_descriptors
        ]
        return {
            "prompt_id": prompt_id,
            "output_path": paths[0],
            "output_paths": paths,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "workflow_path": str(self.workflow_path),
            "mapping": self.workflow.mapping_report(),
            "settings": {
                "width": request.width,
                "height": request.height,
                "frames": request.frames,
                "steps": request.steps,
                "fps": request.fps,
                "seed": request.seed,
            },
        }
