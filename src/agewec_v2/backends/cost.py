"""動画生成の課金ガード。

【本番経路: 現役】外部APIへ課金リクエストを送る前に、必ずここを通す。

守るべき不変条件（リトライによる予算超過を防ぐ）:

    実課金累計 + 次回見積額 <= 承認上限

見積と実課金を **別々に** 管理する。多くのAPIは要求尺を切り上げて課金するため
（6秒要求 → 8秒課金）、見積だけを積んでいくと実額とずれるため。

H2（重い生成を始める直前のゲート）で全体見積を1回だけ人間へ提示し、
以降の再生成では自動でこの検査だけを行う（都度承認は求めない）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class VideoBudgetExceededError(RuntimeError):
    """上限を超えるため生成を実行しなかった。"""


@dataclass
class BudgetStatus:
    limit_usd: float
    spent_usd: float           # 実課金の累計
    next_estimate_usd: float   # 次の1本の見積
    generations: int

    @property
    def projected_usd(self) -> float:
        return round(self.spent_usd + self.next_estimate_usd, 4)

    @property
    def allowed(self) -> bool:
        return self.projected_usd <= self.limit_usd + 1e-9

    @property
    def remaining_usd(self) -> float:
        return round(max(0.0, self.limit_usd - self.spent_usd), 4)


class VideoCostGuard:
    """実課金累計を台帳に持ち、送信前に上限を検査する。"""

    def __init__(self, ledger_path: Path, limit_usd: float) -> None:
        self.ledger_path = Path(ledger_path)
        self.limit_usd = float(limit_usd)

    # ---------------------------------------------------------------- 台帳
    def _read(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"spent_usd": 0.0, "generations": [], "estimated_usd": 0.0}
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"spent_usd": 0.0, "generations": [], "estimated_usd": 0.0}

    def _write(self, ledger: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def spent_usd(self) -> float:
        return round(float(self._read().get("spent_usd", 0.0)), 4)

    # ------------------------------------------------------------ 事前検査
    def check(self, next_estimate_usd: float) -> BudgetStatus:
        """送信前の検査。実行はせず、状態だけ返す。"""
        ledger = self._read()
        return BudgetStatus(
            limit_usd=self.limit_usd,
            spent_usd=round(float(ledger.get("spent_usd", 0.0)), 4),
            next_estimate_usd=round(float(next_estimate_usd), 4),
            generations=len(ledger.get("generations", [])),
        )

    def ensure(self, next_estimate_usd: float) -> BudgetStatus:
        """上限を超えるなら例外を投げる（＝APIへ送信させない）。"""
        status = self.check(next_estimate_usd)
        if not status.allowed:
            raise VideoBudgetExceededError(
                f"予算上限に到達: 実課金 ${status.spent_usd:.2f} "
                f"+ 見積 ${status.next_estimate_usd:.2f} "
                f"> 上限 ${status.limit_usd:.2f}"
            )
        return status

    # ------------------------------------------------------------ 実績記録
    def record(
        self,
        *,
        cut_id: int,
        provider: str,
        model: str,
        cost_usd: float,
        billed_seconds: float,
        job_id: str | None = None,
        estimated_usd: float | None = None,
    ) -> float:
        """生成後に実課金を積む。累計を返す。"""
        ledger = self._read()
        ledger["spent_usd"] = round(
            float(ledger.get("spent_usd", 0.0)) + float(cost_usd), 6
        )
        if estimated_usd is not None:
            ledger["estimated_usd"] = round(
                float(ledger.get("estimated_usd", 0.0)) + float(estimated_usd),
                6,
            )
        ledger.setdefault("generations", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "cut_id": cut_id,
                "provider": provider,
                "model": model,
                "job_id": job_id,
                "billed_seconds": billed_seconds,
                "estimated_usd": estimated_usd,
                "cost_usd": round(float(cost_usd), 6),
            }
        )
        self._write(ledger)
        return ledger["spent_usd"]


def estimate_run_cost(
    capabilities: Any, cuts: list[dict[str, Any]]
) -> dict[str, Any]:
    """H2 で人間へ提示する、実行全体の概算。"""
    lines = []
    total = 0.0
    for cut in cuts:
        requested = float(cut.get("seconds", 0))
        billed = capabilities.resolve_seconds(requested)
        cost = billed * capabilities.cost_per_second_usd
        total += cost
        lines.append(
            {
                "cut_id": cut.get("id"),
                "requested_seconds": requested,
                "billed_seconds": billed,
                "cost_usd": round(cost, 4),
            }
        )
    return {
        "model": capabilities.model,
        "cost_per_second_usd": capabilities.cost_per_second_usd,
        "cuts": lines,
        "total_usd": round(total, 4),
    }
