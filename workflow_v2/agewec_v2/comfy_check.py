"""Non-generating validation for ComfyUI and the exported API workflow."""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .backends.comfy_runtime import ComfyClient
from .paths import WORKFLOW_ROOT

ROOT = WORKFLOW_ROOT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config_llm.yaml",
    )
    parser.add_argument("--workflow", type=Path)
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    comfy = config.get("comfy", {})
    workflow_path = args.workflow or (
        ROOT / comfy.get("workflow_api_json", "workflows/ltx_i2v_api.json")
    )
    client = ComfyClient(
        base_url=comfy.get("base_url", "http://127.0.0.1:8188"),
        workflow_path=workflow_path,
        input_mapping=comfy.get("inputs", {}),
        output_dir=ROOT / "work" / "production",
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )
    print("base_url:", client.base_url)
    print("workflow:", workflow_path)
    try:
        client.workflow.mapping_report()
        report = client.preflight()
    except Exception as exc:
        print("status: failed")
        print("error:", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    print("status: ready")
    print("node_count:", report["node_count"])
    print("input_mapping:")
    for name, target in report["mapping"].items():
        print(
            f"  {name}: node={target['node_id']} "
            f"class={target['class_type']} input={target['input']}"
        )


if __name__ == "__main__":
    main()
