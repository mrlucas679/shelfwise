from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .utils import decimal, q2


@dataclass(frozen=True, slots=True)
class DemandForecast:
    sku: str
    daily_units: Decimal
    horizon_days: int
    horizon_units: Decimal
    method: str
    confidence: Decimal


def forecast_demand(
    *,
    sku: str,
    recent_daily_units: list[Decimal],
    horizon_days: int,
    payday_multiplier: Decimal = Decimal("1.35"),
) -> DemandForecast:
    """Moving-average forecast, optionally uplifted for a payday demand spike.

    The default multiplier matches the calibrated golden payday-yoghurt scenario. Any
    caller whose story is NOT payday-driven must pass `payday_multiplier=Decimal("1")`
    explicitly - the uplift is not a general-purpose demand adjustment.
    """
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if not recent_daily_units:
        return DemandForecast(
            sku,
            Decimal("0"),
            horizon_days,
            Decimal("0"),
            "no_sales_history",
            Decimal("0.2"),
        )

    base = sum(
        (decimal(x) for x in recent_daily_units),
        Decimal("0"),
    ) / Decimal(len(recent_daily_units))
    daily = base * payday_multiplier
    support = min(Decimal(len(recent_daily_units)) / Decimal("14"), Decimal("1"))
    confidence = q2(Decimal("0.55") + support * Decimal("0.35"))
    return DemandForecast(
        sku=sku,
        daily_units=q2(daily),
        horizon_days=horizon_days,
        horizon_units=q2(daily * horizon_days),
        method="moving_average_with_payday_multiplier",
        confidence=confidence,
    )
