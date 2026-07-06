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
