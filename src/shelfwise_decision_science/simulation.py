from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from shelfwise_contracts import Money

from .utils import decimal, q2


@dataclass(frozen=True, slots=True)
class MarkdownSimulation:
    sku: str
    markdown_units_sold: Decimal
    hold_units_sold: Decimal
    markdown_waste_units: Decimal
    hold_waste_units: Decimal
    markdown_profit: Money
    hold_profit: Money
    incremental_profit: Money
    method: str
    confidence: Decimal


def simulate_markdown(
    *,
    sku: str,
    units_on_hand: Decimal,
    days_to_expiry: Decimal,
    base_daily_units: Decimal,
    unit_price: Money,
    unit_cost: Money,
    discount_pct: Decimal,
    demand_elasticity: Decimal = Decimal("1.5"),
) -> MarkdownSimulation:
    if discount_pct < 0 or discount_pct >= 1:
        raise ValueError("discount_pct must be in [0, 1)")

    units = decimal(units_on_hand)
    days = decimal(days_to_expiry)
    hold_units = min(units, decimal(base_daily_units) * days)
    markdown_daily = decimal(base_daily_units) * (Decimal("1") + demand_elasticity * discount_pct)
    markdown_units = min(units, markdown_daily * days)
    hold_waste = max(units - hold_units, Decimal("0"))
    markdown_waste = max(units - markdown_units, Decimal("0"))

    hold_margin = unit_price - unit_cost
    markdown_price = unit_price * (Decimal("1") - discount_pct)
    markdown_margin = markdown_price - unit_cost
    hold_profit = (hold_margin * hold_units) - (unit_cost * hold_waste)
    markdown_profit = (markdown_margin * markdown_units) - (unit_cost * markdown_waste)

    return MarkdownSimulation(
        sku=sku,
        markdown_units_sold=q2(markdown_units),
        hold_units_sold=q2(hold_units),
        markdown_waste_units=q2(markdown_waste),
        hold_waste_units=q2(hold_waste),
        markdown_profit=markdown_profit,
        hold_profit=hold_profit,
        incremental_profit=markdown_profit - hold_profit,
        method="deterministic_markdown_vs_hold_expected_value",
        confidence=Decimal("0.80"),
    )
