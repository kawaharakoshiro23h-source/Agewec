"""Non-billable connectivity check for the configured LLM server."""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import yaml

from .llm.config import LLMSettings


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config_llm.yaml",
    )
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    settings = LLMSettings.from_sources(config)

    print("enabled:", settings.enabled)
    print("provider:", settings.provider)
    print("base_url:", settings.base_url)
    print("model:", settings.model)
    print("api_key_configured:", bool(settings.api_key))
    if not settings.enabled:
        print("status: disabled")
        return

    try:
        response = httpx.get(
            f"{settings.base_url}/models",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=min(settings.timeout_seconds, 10.0),
        )
        response.raise_for_status()
        body = response.json()
        model_ids = [
            item.get("id")
            for item in body.get("data", [])
            if isinstance(item, dict)
        ]
        print("status: connected")
        print("configured_model_available:", settings.model in model_ids)
        print("available_model_count:", len(model_ids))
    except Exception as exc:
        print("status: connection_failed")
        print("error:", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
