from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agewec_v2.backends.comfy_runtime import (
    ComfyClient,
    ComfyGenerationRequest,
    ComfyWorkflow,
    ComfyWorkflowError,
)


def api_workflow() -> dict:
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "input.png"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "positive", "clip": ["10", 0]},
            "_meta": {"title": "CLIP Text Encode (Positive Prompt)"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "negative", "clip": ["10", 0]},
            "_meta": {"title": "CLIP Text Encode (Negative Prompt)"},
        },
        "4": {
            "class_type": "LTXVConditioning",
            "inputs": {"positive": ["2", 0], "negative": ["3", 0]},
        },
        "5": {
            "class_type": "LTXVImgToVideo",
            "inputs": {
                "width": 768,
                "height": 512,
                "length": 97,
                "positive": ["4", 0],
            },
        },
        "6": {
            "class_type": "LTXVScheduler",
            "inputs": {"steps": 30, "latent": ["5", 0]},
        },
        "7": {
            "class_type": "SamplerCustom",
            "inputs": {"noise_seed": 42, "sigmas": ["6", 0]},
        },
        "8": {
            "class_type": "CreateVideo",
            "inputs": {"fps": 24, "images": ["11", 0]},
        },
        "9": {
            "class_type": "SaveVideo",
            "inputs": {"filename_prefix": "video/test", "video": ["8", 0]},
        },
        "10": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": "t5.safetensors"},
        },
        "11": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["7", 0]},
        },
    }


class FakeComfyHandler(BaseHTTPRequestHandler):
    workflow_received: dict | None = None

    def _json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/system_stats":
            self._json({"system": {"os": "test"}, "devices": []})
            return
        if path == "/object_info":
            self._json(
                {
                    node["class_type"]: {}
                    for node in api_workflow().values()
                }
            )
            return
        if path == "/history/prompt-1":
            self._json(
                {
                    "prompt-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "videos": [
                                    {
                                        "filename": "result.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            )
            return
        if path == "/view":
            body = b"fake-mp4"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/upload/image":
            self._json({"name": "uploaded.png", "subfolder": "", "type": "input"})
            return
        if self.path == "/prompt":
            payload = json.loads(body)
            self.__class__.workflow_received = payload["prompt"]
            self._json({"prompt_id": "prompt-1", "node_errors": {}})
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


class ComfyRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        FakeComfyHandler.workflow_received = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_auto_discovers_ltx_inputs(self) -> None:
        workflow = ComfyWorkflow(api_workflow())
        report = workflow.mapping_report()
        self.assertEqual(report["image"]["node_id"], "1")
        self.assertEqual(report["positive_prompt"]["node_id"], "2")
        self.assertEqual(report["negative_prompt"]["node_id"], "3")
        self.assertEqual(report["frames"]["input"], "length")
        self.assertEqual(report["seed"]["input"], "noise_seed")

    def test_rejects_ui_format_with_actionable_message(self) -> None:
        with self.assertRaisesRegex(ComfyWorkflowError, "UI-format"):
            ComfyWorkflow({"nodes": [], "links": []})

    def test_full_http_generation_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(api_workflow()))
            image_path = root / "input.png"
            image_path.write_bytes(b"fake-image")
            client = ComfyClient(
                base_url=f"http://127.0.0.1:{self.server.server_port}",
                workflow_path=workflow_path,
                input_mapping={},
                output_dir=root / "output",
                poll_interval=0.01,
                timeout=2,
            )
            result = client.generate(
                ComfyGenerationRequest(
                    image_path=str(image_path),
                    positive_prompt="Kitakyushu night view",
                    width=576,
                    height=384,
                    frames=49,
                    steps=20,
                    fps=24,
                    seed=123,
                )
            )
            self.assertTrue(Path(result["output_path"]).exists())
            received = FakeComfyHandler.workflow_received
            assert received is not None
            self.assertEqual(received["1"]["inputs"]["image"], "uploaded.png")
            self.assertEqual(
                received["2"]["inputs"]["text"],
                "Kitakyushu night view",
            )
            self.assertEqual(received["5"]["inputs"]["width"], 576)
            self.assertEqual(received["5"]["inputs"]["length"], 49)
            self.assertEqual(received["6"]["inputs"]["steps"], 20)
            self.assertEqual(received["7"]["inputs"]["noise_seed"], 123)


if __name__ == "__main__":
    unittest.main()
