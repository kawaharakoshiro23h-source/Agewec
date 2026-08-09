"""Non-billable connectivity check for the configured LLM server."""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import yaml

from .llm.config import LLMSettings
from .llm.cost_guard import LLMCostGuard
from .paths import CONFIG_ROOT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_ROOT / "config_llm.yaml",
    )
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    settings = LLMSettings.from_sources(config)

    print("enabled:", settings.enabled)
    print("provider:", settings.provider)
    print("base_url:", settings.base_url)
    print("model:", settings.model)
    print("api_key_configured:", bool(settings.api_key))
    print("cost_guard_enabled:", settings.cost_guard_enabled)
    if settings.provider == "openai" and settings.cost_guard_enabled:
        guard = LLMCostGuard(
            ledger_path=settings.cost_ledger_path,
            limit_usd=settings.cost_limit_usd,
            pricing_model=settings.pricing_model,
            input_cost_per_million_usd=(
                settings.input_cost_per_million_usd
            ),
            output_cost_per_million_usd=(
                settings.output_cost_per_million_usd
            ),
        )
        budget = guard.snapshot()
        print("cost_ledger:", settings.cost_ledger_path)
        print("cost_spent_usd:", budget["spent_usd"])
        print(
            "cost_remaining_usd:",
            budget["remaining_budget_usd"],
        )
        print("cost_limit_usd:", budget["budget_limit_usd"])
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
