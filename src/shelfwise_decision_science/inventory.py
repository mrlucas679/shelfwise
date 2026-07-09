from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from math import erf, sqrt

from shelfwise_contracts import Money

from .utils import clamp, decimal, q2, safe_div

_SERVICE_LEVEL_Z: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0.50"), Decimal("0.0000")),
    (Decimal("0.75"), Decimal("0.6745")),
    (Decimal("0.80"), Decimal("0.8416")),
    (Decimal("0.85"), Decimal("1.0364")),
    (Decimal("0.90"), Decimal("1.2816")),
    (Decimal("0.95"), Decimal("1.6449")),
    (Decimal("0.975"), Decimal("1.9600")),
    (Decimal("0.99"), Decimal("2.3263")),
)


@dataclass(frozen=True, slots=True)
class InventoryPolicyInput:
    sku: str
    on_hand: Decimal
    avg_daily_demand: Decimal
    demand_std: Decimal
    lead_time_days: Decimal
    unit_cost: Money
    committed_units: Decimal = Decimal("0")
    service_level: Decimal = Decimal("0.95")


@dataclass(frozen=True, slots=True)
class ReorderPolicy:
    sku: str
    available_to_sell_units: Decimal
    safety_stock_units: Decimal
    reorder_point_units: Decimal
    lead_time_demand_units: Decimal
    stockout_risk: Decimal
    risk_band: str
    units_below_reorder: Decimal
    should_reorder: bool
    suggested_order_units: Decimal
    zar_exposure: Money
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "available_to_sell_units": str(self.available_to_sell_units),
            "safety_stock_units": str(self.safety_stock_units),
            "reorder_point_units": str(self.reorder_point_units),
            "lead_time_demand_units": str(self.lead_time_demand_units),
            "stockout_risk": str(self.stockout_risk),
            "risk_band": self.risk_band,
            "units_below_reorder": str(self.units_below_reorder),
            "should_reorder": self.should_reorder,
            "suggested_order_units": str(self.suggested_order_units),
            "zar_exposure": self.zar_exposure.to_dict(),
            "method": self.method,
        }


def z_for_service_level(service_level: Decimal) -> Decimal:
    sl = clamp(service_level, low=_SERVICE_LEVEL_Z[0][0], high=_SERVICE_LEVEL_Z[-1][0])
    for (lo_sl, lo_z), (hi_sl, hi_z) in pairwise(_SERVICE_LEVEL_Z):
        if lo_sl <= sl <= hi_sl:
            frac = Decimal("0") if hi_sl == lo_sl else (sl - lo_sl) / (hi_sl - lo_sl)
            return (lo_z + frac * (hi_z - lo_z)).quantize(Decimal("0.0001"))
    return _SERVICE_LEVEL_Z[-1][1]


def compute_reorder_policy(data: InventoryPolicyInput) -> ReorderPolicy:
    _reject_negative(data.on_hand, "on_hand")
    _reject_negative(data.avg_daily_demand, "avg_daily_demand")
    _reject_negative(data.demand_std, "demand_std")
    _reject_negative(data.lead_time_days, "lead_time_days")
    _reject_negative(data.committed_units, "committed_units")

    lead = decimal(data.lead_time_days)
    z_factor = z_for_service_level(decimal(data.service_level))
    sigma_lead = decimal(data.demand_std) * (lead.sqrt() if lead > 0 else Decimal("0"))
    safety_stock = z_factor * sigma_lead
    lead_demand = decimal(data.avg_daily_demand) * lead
    reorder_point = lead_demand + safety_stock
    available = max(decimal(data.on_hand) - decimal(data.committed_units), Decimal("0"))

    if sigma_lead == 0:
        stockout_risk = Decimal("1") if available < lead_demand else Decimal("0")
    else:
        lead_time_z = safe_div(available - lead_demand, sigma_lead)
        stockout_risk = clamp(Decimal("1") - _normal_cdf(lead_time_z))

    units_below = max(reorder_point - available, Decimal("0"))
    expected_short = max(lead_demand - available, Decimal("0"))
    should_reorder = available <= reorder_point
    # At exact equality (available == reorder_point) units_below rounds to zero, which
    # would otherwise report "reorder now, order zero units" - an actionable trigger must
    # suggest at least one unit, not a self-contradicting no-op.
    suggested_order_units = units_below if units_below > 0 else (
        Decimal("1") if should_reorder else Decimal("0")
    )
    return ReorderPolicy(
        sku=data.sku,
        available_to_sell_units=q2(available),
        safety_stock_units=q2(safety_stock),
        reorder_point_units=q2(reorder_point),
        lead_time_demand_units=q2(lead_demand),
        stockout_risk=q2(stockout_risk),
        risk_band=_risk_band(stockout_risk),
        units_below_reorder=q2(units_below),
        should_reorder=should_reorder,
        suggested_order_units=q2(suggested_order_units),
        zar_exposure=data.unit_cost * expected_short,
        method="safety_stock_reorder_point_normal_lead_time_demand",
    )


def _normal_cdf(value: Decimal) -> Decimal:
    return decimal(0.5 * (1.0 + erf(float(value) / sqrt(2.0))))


def _risk_band(value: Decimal) -> str:
    risk = decimal(value)
    if risk >= Decimal("0.75"):
        return "critical"
    if risk >= Decimal("0.45"):
        return "high"
    if risk >= Decimal("0.20"):
        return "medium"
    return "low"


def _reject_negative(value: Decimal, field_name: str) -> None:
    if decimal(value) < 0:
        raise ValueError(f"{field_name} cannot be negative")
