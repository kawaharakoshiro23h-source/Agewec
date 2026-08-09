"""Local, fail-closed cumulative cost guard for paid LLM requests."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()


class LLMBudgetExceededError(RuntimeError):
    """Raised before a request that could exceed the configured local budget."""


@dataclass(frozen=True)
class CostReservation:
    reservation_id: str
    reserved_usd: float


class LLMCostGuard:
    def __init__(
        self,
        *,
        ledger_path: Path,
        limit_usd: float,
        pricing_model: str,
        input_cost_per_million_usd: float,
        output_cost_per_million_usd: float,
    ) -> None:
        self.ledger_path = ledger_path
        self.limit_usd = float(limit_usd)
        self.pricing_model = pricing_model
        self.input_rate = float(input_cost_per_million_usd)
        self.output_rate = float(output_cost_per_million_usd)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "currency": "USD",
            "pricing_model": self.pricing_model,
            "limit_usd": self.limit_usd,
            "spent_usd": 0.0,
            "reserved_usd": 0.0,
            "reservations": {},
            "requests": [],
            "updated_at": self._now(),
        }

    def _read(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return self._empty()
        try:
            ledger = json.loads(
                self.ledger_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"LLM cost ledger is unreadable: {self.ledger_path}"
            ) from exc
        if ledger.get("pricing_model") != self.pricing_model:
            raise RuntimeError(
                "LLM cost ledger pricing model differs from configuration: "
                f"{ledger.get('pricing_model')} != {self.pricing_model}"
            )
        return ledger

    def _write(self, ledger: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger["limit_usd"] = self.limit_usd
        ledger["updated_at"] = self._now()
        temporary = self.ledger_path.with_name(
            f".{self.ledger_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.ledger_path)

    def _estimate_reservation(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> float:
        # UTF-8 bytes are a conservative upper estimate for input tokens.
        input_upper_bound = len(
            (system_prompt + user_prompt).encode("utf-8")
        )
        return (
            input_upper_bound * self.input_rate
            + max_output_tokens * self.output_rate
        ) / 1_000_000

    def reserve(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> CostReservation:
        reserved = self._estimate_reservation(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=max_output_tokens,
        )
        with _LOCK:
            ledger = self._read()
            spent = float(ledger.get("spent_usd", 0.0))
            outstanding = float(ledger.get("reserved_usd", 0.0))
            projected = spent + outstanding + reserved
            if projected > self.limit_usd:
                remaining = max(0.0, self.limit_usd - spent - outstanding)
                raise LLMBudgetExceededError(
                    "OpenAI local cost guard blocked the request before send: "
                    f"spent=${spent:.6f}, reserved=${outstanding:.6f}, "
                    f"request_max=${reserved:.6f}, "
                    f"remaining=${remaining:.6f}, limit=${self.limit_usd:.2f}"
                )
            reservation_id = uuid.uuid4().hex
            ledger.setdefault("reservations", {})[reservation_id] = {
                "model": model,
                "reserved_usd": reserved,
                "created_at": self._now(),
            }
            ledger["reserved_usd"] = round(outstanding + reserved, 10)
            self._write(ledger)
        return CostReservation(reservation_id, reserved)

    def snapshot(self) -> dict[str, float]:
        with _LOCK:
            ledger = self._read()
            spent = float(ledger.get("spent_usd", 0.0))
            reserved = float(ledger.get("reserved_usd", 0.0))
            return {
                "spent_usd": spent,
                "reserved_usd": reserved,
                "remaining_budget_usd": round(
                    max(0.0, self.limit_usd - spent - reserved),
                    10,
                ),
                "budget_limit_usd": self.limit_usd,
            }

    def settle(
        self,
        reservation: CostReservation,
        *,
        usage: dict[str, Any] | None,
        status: str,
    ) -> dict[str, float]:
        with _LOCK:
            ledger = self._read()
            entry = ledger.setdefault("reservations", {}).pop(
                reservation.reservation_id,
                None,
            )
            if entry is None:
                raise RuntimeError(
                    "Unknown LLM cost reservation: "
                    f"{reservation.reservation_id}"
                )
            reserved = float(entry["reserved_usd"])
            prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
            completion_tokens = int(
                (usage or {}).get("completion_tokens") or 0
            )
            usage_available = prompt_tokens > 0 or completion_tokens > 0
            actual = (
                (
                    prompt_tokens * self.input_rate
                    + completion_tokens * self.output_rate
                )
                / 1_000_000
                if usage_available
                else reserved
            )
            # The preflight estimate is deliberately conservative. Record the
            # provider-reported amount even if it unexpectedly exceeds it.
            charged = actual
            spent = round(
                float(ledger.get("spent_usd", 0.0)) + charged,
                10,
            )
            ledger["spent_usd"] = spent
            ledger["reserved_usd"] = round(
                max(
                    0.0,
                    float(ledger.get("reserved_usd", 0.0)) - reserved,
                ),
                10,
            )
            ledger.setdefault("requests", []).append(
                {
                    "reservation_id": reservation.reservation_id,
                    "model": entry["model"],
                    "status": status,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "reserved_usd": reserved,
                    "charged_usd": charged,
                    "usage_available": usage_available,
                    "completed_at": self._now(),
                }
            )
            self._write(ledger)
            result = {
                "request_cost_usd": charged,
                "cumulative_cost_usd": spent,
                "remaining_budget_usd": round(
                    max(
                        0.0,
                        self.limit_usd
                        - spent
                        - float(ledger["reserved_usd"]),
                    ),
                    10,
                ),
                "budget_limit_usd": self.limit_usd,
            }
            if spent > self.limit_usd:
                raise LLMBudgetExceededError(
                    "Provider usage unexpectedly exceeded the reserved amount; "
                    f"recorded cumulative=${spent:.6f}, "
                    f"limit=${self.limit_usd:.2f}"
                )
            return result
