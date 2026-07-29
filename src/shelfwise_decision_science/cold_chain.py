from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .utils import clamp, decimal, q2, safe_div


@dataclass(frozen=True, slots=True)
class ColdChainRisk:
    area: str
    risk: Decimal
    penalty_days: Decimal
    method: str
    confidence: Decimal

    def __post_init__(self) -> None:
        """State the postcondition explicitly instead of trusting `clamp()` silently.

        Mirrors `ExpiryRisk.__post_init__` - `score_cold_chain_risk` feeds `risk` and
        `penalty_days` straight into `score_expiry_risk`'s effective-days math, so a
        silently out-of-range value here would corrupt an expiry decision downstream
        without either function's own tests necessarily catching it.
        """
        if not (Decimal("0") <= self.risk <= Decimal("1")):
            raise ValueError(f"ColdChainRisk.risk out of [0,1]: {self.risk}")
        if self.penalty_days < 0:
            raise ValueError(f"ColdChainRisk.penalty_days must be >= 0: {self.penalty_days}")
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise ValueError(f"ColdChainRisk.confidence out of [0,1]: {self.confidence}")


def score_cold_chain_risk(
    *,
    area: str,
    outage_hours: Decimal,
    average_temp_c: Decimal,
    safe_hold_hours: Decimal = Decimal("2"),
    max_safe_c: Decimal = Decimal("5"),
    hard_unsafe_c: Decimal = Decimal("8"),
) -> ColdChainRisk:
    outage_excess = max(decimal(outage_hours) - safe_hold_hours, Decimal("0"))
    temp_excess = max(decimal(average_temp_c) - max_safe_c, Decimal("0"))
    temp_span = max(hard_unsafe_c - max_safe_c, Decimal("0.1"))
    dose = safe_div(outage_excess, safe_hold_hours) + safe_div(temp_excess, temp_span)
    risk = clamp(dose / Decimal("2"))
    return ColdChainRisk(
        area=area,
        risk=q2(risk),
        penalty_days=q2(risk * Decimal("3")),
        method="thermal_dose_time_to_unsafe_heuristic",
        confidence=Decimal("0.78"),
    )
