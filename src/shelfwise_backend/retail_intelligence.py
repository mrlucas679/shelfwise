"""Reusable retail operations contracts and deterministic calculations.

This module is the runtime home for FEFO, delivery, supplier-cover, and learning math.
The legacy CSV compatibility package remains available for migration tests, but the
application must not import it to calculate live tenant facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from pathlib import Path
from typing import Any

from shelfwise_contracts import Money


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _q2(value: object) -> Decimal:
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _non_negative(value: int, field_name: str) -> int:
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class SeededScenario:
    """Compatibility-shaped scenario facts backed by the generated world."""

    sku: str
    product_name: str
    category: str
    supplier: str
    location: str
    units_on_hand: int
    reorder_point: int
    days_to_expiry: int
    recent_daily_units: tuple[Decimal, ...]
    unit_cost: Money
    unit_price: Money
    supplier_lead_time_days: Decimal
    supplier_recent_delay: bool
    datasets_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "product_name": self.product_name,
            "category": self.category,
            "supplier": self.supplier,
            "location": self.location,
            "units_on_hand": self.units_on_hand,
            "reorder_point": self.reorder_point,
            "days_to_expiry": self.days_to_expiry,
            "recent_daily_units": [str(value) for value in self.recent_daily_units],
            "unit_cost": self.unit_cost.to_dict(),
            "unit_price": self.unit_price.to_dict(),
            "supplier_lead_time_days": str(self.supplier_lead_time_days),
            "supplier_recent_delay": self.supplier_recent_delay,
            "datasets_dir": str(self.datasets_dir),
        }


@dataclass(frozen=True, slots=True)
class StockBatch:
    sku: str
    lot: str
    units: int
    expiry_date: date
    received_date: date
    location: str

    def __post_init__(self) -> None:
        _non_negative(self.units, "units")


@dataclass(frozen=True, slots=True)
class BatchPosition:
    sku: str
    lot: str
    units: int
    expiry_date: date
    received_date: date
    location: str
    days_to_expiry: int
    stock_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "lot": self.lot,
            "units": self.units,
            "expiry_date": self.expiry_date.isoformat(),
            "received_date": self.received_date.isoformat(),
            "location": self.location,
            "days_to_expiry": self.days_to_expiry,
            "stock_status": self.stock_status,
        }


@dataclass(frozen=True, slots=True)
class FefoStockSplit:
    sku: str
    as_of: date
    total_units: int
    priority_sell_units: int
    normal_units: int
    blocked_units: int
    fefo_batches: tuple[BatchPosition, ...]
    conclusion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "as_of": self.as_of.isoformat(),
            "total_units": self.total_units,
            "priority_sell_units": self.priority_sell_units,
            "normal_units": self.normal_units,
            "blocked_units": self.blocked_units,
            "fefo_batches": [item.to_dict() for item in self.fefo_batches],
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    sku: str
    ordered_units: int
    asn_units: int
    received_units: int
    accepted_units: int
    rejected_units: int = 0
    short_dated_units: int = 0

    def __post_init__(self) -> None:
        for name in (
            "ordered_units",
            "asn_units",
            "received_units",
            "accepted_units",
            "rejected_units",
            "short_dated_units",
        ):
            _non_negative(getattr(self, name), name)
        if self.accepted_units + self.rejected_units > self.received_units:
            raise ValueError("accepted_units plus rejected_units cannot exceed received_units")
        if self.short_dated_units > self.received_units:
            raise ValueError("short_dated_units cannot exceed received_units")


@dataclass(frozen=True, slots=True)
class DeliveryReconciliation:
    sku: str
    ordered_units: int
    asn_units: int
    received_units: int
    accepted_units: int
    rejected_units: int
    missing_units: int
    over_units: int
    short_dated_units: int
    supplier_fill_rate: Decimal
    status: str
    conclusion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "ordered_units": self.ordered_units,
            "asn_units": self.asn_units,
            "received_units": self.received_units,
            "accepted_units": self.accepted_units,
            "rejected_units": self.rejected_units,
            "missing_units": self.missing_units,
            "over_units": self.over_units,
            "short_dated_units": self.short_dated_units,
            "supplier_fill_rate": str(self.supplier_fill_rate),
            "status": self.status,
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True, slots=True)
class SupplierCoverRequest:
    sku: str
    units_on_hand: int
    forecast_daily_units: Decimal
    supplier_lead_time_days: Decimal
    transfer_available_units: int = 0

    def __post_init__(self) -> None:
        _non_negative(self.units_on_hand, "units_on_hand")
        _non_negative(self.transfer_available_units, "transfer_available_units")
        if _decimal(self.forecast_daily_units) <= 0:
            raise ValueError("forecast_daily_units must be positive")
        if _decimal(self.supplier_lead_time_days) <= 0:
            raise ValueError("supplier_lead_time_days must be positive")


@dataclass(frozen=True, slots=True)
class SupplierCoverPlan:
    sku: str
    units_on_hand: int
    forecast_daily_units: Decimal
    supplier_lead_time_days: Decimal
    days_of_supply: Decimal
    units_needed_until_delivery: int
    gap_before_delivery_units: int
    transfer_available_units: int
    transfer_units_recommended: int
    recommended_action: str
    conclusion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "units_on_hand": self.units_on_hand,
            "forecast_daily_units": str(self.forecast_daily_units),
            "supplier_lead_time_days": str(self.supplier_lead_time_days),
            "days_of_supply": str(self.days_of_supply),
            "units_needed_until_delivery": self.units_needed_until_delivery,
            "gap_before_delivery_units": self.gap_before_delivery_units,
            "transfer_available_units": self.transfer_available_units,
            "transfer_units_recommended": self.transfer_units_recommended,
            "recommended_action": self.recommended_action,
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    sku: str
    action: str
    predicted_sell_through_units: int
    actual_sell_through_units: int
    predicted_waste_units: int
    actual_waste_units: int

    def __post_init__(self) -> None:
        for name in (
            "predicted_sell_through_units",
            "actual_sell_through_units",
            "predicted_waste_units",
            "actual_waste_units",
        ):
            _non_negative(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OutcomeSummary:
    sku: str
    action: str
    sell_through_delta_units: int
    waste_delta_units: int
    score: Decimal
    lesson: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "action": self.action,
            "sell_through_delta_units": self.sell_through_delta_units,
            "waste_delta_units": self.waste_delta_units,
            "score": str(self.score),
            "lesson": self.lesson,
        }


def split_stock_by_fefo(
    *,
    sku: str,
    batches: tuple[StockBatch, ...],
    as_of: date,
    priority_window_days: int = 1,
) -> FefoStockSplit:
    if priority_window_days < 0:
        raise ValueError("priority_window_days must be non-negative")
    if not batches:
        raise ValueError("batches cannot be empty")
    if any(batch.sku != sku for batch in batches):
        raise ValueError("all batches must match sku")
    positions: list[BatchPosition] = []
    priority = normal = blocked = 0
    for batch in sorted(batches, key=lambda item: (item.expiry_date, item.received_date, item.lot)):
        days = (batch.expiry_date - as_of).days
        if days < 0:
            status = "blocked"
            blocked += batch.units
        elif days <= priority_window_days:
            status = "priority_sell"
            priority += batch.units
        else:
            status = "normal"
            normal += batch.units
        positions.append(
            BatchPosition(
                sku=batch.sku,
                lot=batch.lot,
                units=batch.units,
                expiry_date=batch.expiry_date,
                received_date=batch.received_date,
                location=batch.location,
                days_to_expiry=days,
                stock_status=status,
            )
        )
    total = priority + normal + blocked
    return FefoStockSplit(
        sku=sku,
        as_of=as_of,
        total_units=total,
        priority_sell_units=priority,
        normal_units=normal,
        blocked_units=blocked,
        fefo_batches=tuple(positions),
        conclusion=(
            f"{sku} has {total} units: {priority} sell first, "
            f"{normal} normal, {blocked} blocked."
        ),
    )


def reconcile_delivery(receipt: DeliveryReceipt) -> DeliveryReconciliation:
    missing = max(receipt.ordered_units - receipt.received_units, 0)
    over = max(receipt.received_units - receipt.ordered_units, 0)
    fill_rate = (
        Decimal("1")
        if receipt.ordered_units == 0
        else _decimal(receipt.received_units) / _decimal(receipt.ordered_units)
    )
    exception = any(
        (missing, over, receipt.rejected_units, receipt.short_dated_units,
         receipt.asn_units != receipt.received_units)
    )
    status = "exception" if exception else "matched"
    if not exception:
        conclusion = f"{receipt.sku} delivery matches the order."
    elif missing:
        conclusion = (
            f"{receipt.sku} delivery is short {missing} units; "
            f"{receipt.received_units} received from {receipt.ordered_units} ordered."
        )
    elif over:
        conclusion = (
            f"{receipt.sku} delivery has {over} extra units; "
            f"{receipt.received_units} received from {receipt.ordered_units} ordered."
        )
    else:
        conclusion = f"{receipt.sku} delivery has receiving exceptions."
    return DeliveryReconciliation(
        sku=receipt.sku,
        ordered_units=receipt.ordered_units,
        asn_units=receipt.asn_units,
        received_units=receipt.received_units,
        accepted_units=receipt.accepted_units,
        rejected_units=receipt.rejected_units,
        missing_units=missing,
        over_units=over,
        short_dated_units=receipt.short_dated_units,
        supplier_fill_rate=_q2(fill_rate),
        status=status,
        conclusion=conclusion,
    )


def plan_supplier_cover(request: SupplierCoverRequest) -> SupplierCoverPlan:
    forecast = _decimal(request.forecast_daily_units)
    lead_time = _decimal(request.supplier_lead_time_days)
    days_supply = _decimal(request.units_on_hand) / forecast
    units_needed = ceil(forecast * lead_time)
    gap = max(units_needed - request.units_on_hand, 0)
    transfer = min(gap, request.transfer_available_units)
    if gap == 0:
        action = "hold"
        conclusion = f"Hold ordering {request.sku}: current stock covers supplier lead time."
    elif transfer:
        action = "transfer"
        conclusion = f"Ordering {request.sku} will arrive too late; transfer {transfer} units now."
    else:
        action = "order_and_warn_gap"
        conclusion = f"Order {request.sku}, but expect a {gap} unit gap before delivery."
    return SupplierCoverPlan(
        sku=request.sku,
        units_on_hand=request.units_on_hand,
        forecast_daily_units=_q2(forecast),
        supplier_lead_time_days=_q2(lead_time),
        days_of_supply=_q2(days_supply),
        units_needed_until_delivery=units_needed,
        gap_before_delivery_units=gap,
        transfer_available_units=request.transfer_available_units,
        transfer_units_recommended=transfer,
        recommended_action=action,
        conclusion=conclusion,
    )


def summarize_outcome(outcome: DecisionOutcome) -> OutcomeSummary:
    sell_delta = outcome.actual_sell_through_units - outcome.predicted_sell_through_units
    waste_delta = outcome.actual_waste_units - outcome.predicted_waste_units
    error = abs(sell_delta) + abs(waste_delta)
    expected = max(outcome.predicted_sell_through_units + outcome.predicted_waste_units, 1)
    score = max(Decimal("0"), Decimal("1") - (_decimal(error) / _decimal(expected)))
    if sell_delta > 0:
        lesson = f"Learned {outcome.sku} sold faster than expected after {outcome.action}."
    elif waste_delta > 0:
        lesson = f"Learned {outcome.sku} wasted more than expected after {outcome.action}."
    else:
        lesson = f"Learned {outcome.sku} outcome matched the recommendation closely."
    return OutcomeSummary(
        sku=outcome.sku,
        action=outcome.action,
        sell_through_delta_units=sell_delta,
        waste_delta_units=waste_delta,
        score=_q2(score),
        lesson=lesson,
    )
