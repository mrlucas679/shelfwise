from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from shelfwise_contracts import Money

from .utils import clamp, decimal, q2, safe_div


@dataclass(frozen=True, slots=True)
class ExpiryRisk:
    sku: str
    risk: Decimal
    waste_units: Decimal
    zar_at_risk: Money
    effective_days_to_expiry: Decimal
    method: str
    confidence: Decimal


def score_expiry_risk(
    *,
    sku: str,
    units_on_hand: Decimal,
    days_to_expiry: Decimal,
    forecast_daily_units: Decimal,
    unit_cost: Money,
    cold_chain_risk: Decimal,
    cold_chain_penalty_days: Decimal,
) -> ExpiryRisk:
    effective_days = max(decimal(days_to_expiry) - decimal(cold_chain_penalty_days), Decimal("0"))
    forecast_daily = max(decimal(forecast_daily_units), Decimal("0.01"))
    sell_through_days = safe_div(units_on_hand, forecast_daily)
    expected_sold = forecast_daily * effective_days
    waste_units = max(decimal(units_on_hand) - expected_sold, Decimal("0"))
    velocity_risk = clamp(safe_div(sell_through_days - effective_days, sell_through_days))
    # Weights must sum to 1.0 for a proper convex blend; cold-chain risk already lowers
    # effective_days above (shrinking shelf life feeds into velocity_risk), so this second
    # term is a further escalation on top of that, not the sole cold-chain signal.
    risk = clamp(velocity_risk * Decimal("0.75") + decimal(cold_chain_risk) * Decimal("0.25"))
    return ExpiryRisk(
        sku=sku,
        risk=q2(risk),
        waste_units=q2(waste_units),
        zar_at_risk=unit_cost * waste_units,
        effective_days_to_expiry=q2(effective_days),
        method="sell_through_hazard_with_cold_chain_penalty",
        confidence=Decimal("0.74"),
    )
