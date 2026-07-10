from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from typing import Any


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _q2(value: object) -> Decimal:
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _positive_units(value: int, field_name: str) -> int:
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class StockBatch:
    sku: str
    lot: str
    units: int
    expiry_date: date
    received_date: date
    location: str

    def __post_init__(self) -> None:
        _positive_units(self.units, "units")


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
            "fefo_batches": [batch.to_dict() for batch in self.fefo_batches],
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
        _positive_units(self.ordered_units, "ordered_units")
        _positive_units(self.asn_units, "asn_units")
        _positive_units(self.received_units, "received_units")
        _positive_units(self.accepted_units, "accepted_units")
        _positive_units(self.rejected_units, "rejected_units")
        _positive_units(self.short_dated_units, "short_dated_units")
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
        _positive_units(self.units_on_hand, "units_on_hand")
        _positive_units(self.transfer_available_units, "transfer_available_units")
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
        _positive_units(self.predicted_sell_through_units, "predicted_sell_through_units")
        _positive_units(self.actual_sell_through_units, "actual_sell_through_units")
        _positive_units(self.predicted_waste_units, "predicted_waste_units")
        _positive_units(self.actual_waste_units, "actual_waste_units")


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
    priority_units = 0
    normal_units = 0
    blocked_units = 0

    for batch in sorted(batches, key=lambda item: (item.expiry_date, item.received_date, item.lot)):
        days_to_expiry = (batch.expiry_date - as_of).days
        if days_to_expiry < 0:
            stock_status = "blocked"
            blocked_units += batch.units
        elif days_to_expiry <= priority_window_days:
            stock_status = "priority_sell"
            priority_units += batch.units
        else:
            stock_status = "normal"
            normal_units += batch.units

        positions.append(
            BatchPosition(
                sku=batch.sku,
                lot=batch.lot,
                units=batch.units,
                expiry_date=batch.expiry_date,
                received_date=batch.received_date,
                location=batch.location,
                days_to_expiry=days_to_expiry,
                stock_status=stock_status,
            )
        )

    total_units = priority_units + normal_units + blocked_units
    conclusion = (
        f"{sku} has {total_units} units: {priority_units} sell first, "
        f"{normal_units} normal, {blocked_units} blocked."
    )
    return FefoStockSplit(
        sku=sku,
        as_of=as_of,
        total_units=total_units,
        priority_sell_units=priority_units,
        normal_units=normal_units,
        blocked_units=blocked_units,
        fefo_batches=tuple(positions),
        conclusion=conclusion,
    )


def reconcile_delivery(receipt: DeliveryReceipt) -> DeliveryReconciliation:
    missing_units = max(receipt.ordered_units - receipt.received_units, 0)
    over_units = max(receipt.received_units - receipt.ordered_units, 0)
    fill_rate = (
        Decimal("1")
        if receipt.ordered_units == 0
        else _decimal(receipt.received_units) / _decimal(receipt.ordered_units)
    )
    has_exception = any(
        [
            missing_units,
            over_units,
            receipt.rejected_units,
            receipt.short_dated_units,
            receipt.asn_units != receipt.received_units,
        ]
    )
    status = "exception" if has_exception else "matched"
    if not has_exception:
        conclusion = f"{receipt.sku} delivery matches the order."
    elif missing_units:
        conclusion = (
            f"{receipt.sku} delivery is short {missing_units} units; "
            f"{receipt.received_units} received from {receipt.ordered_units} ordered."
        )
    elif over_units:
        conclusion = (
            f"{receipt.sku} delivery has {over_units} extra units; "
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
        missing_units=missing_units,
        over_units=over_units,
        short_dated_units=receipt.short_dated_units,
        supplier_fill_rate=_q2(fill_rate),
        status=status,
        conclusion=conclusion,
    )


def plan_supplier_cover(request: SupplierCoverRequest) -> SupplierCoverPlan:
    forecast_daily = _decimal(request.forecast_daily_units)
    lead_time_days = _decimal(request.supplier_lead_time_days)
    days_of_supply = _decimal(request.units_on_hand) / forecast_daily
    units_needed = ceil(forecast_daily * lead_time_days)
    gap_units = max(units_needed - request.units_on_hand, 0)
    transfer_units = min(gap_units, request.transfer_available_units)

    if gap_units == 0:
        action = "hold"
        conclusion = (
            f"Hold ordering {request.sku}: current stock covers supplier lead time."
        )
    elif transfer_units:
        action = "transfer"
        conclusion = (
            f"Ordering {request.sku} will arrive too late; transfer {transfer_units} units now."
        )
    else:
        action = "order_and_warn_gap"
        conclusion = (
            f"Order {request.sku}, but expect a {gap_units} unit gap before delivery."
        )

    return SupplierCoverPlan(
        sku=request.sku,
        units_on_hand=request.units_on_hand,
        forecast_daily_units=_q2(forecast_daily),
        supplier_lead_time_days=_q2(lead_time_days),
        days_of_supply=_q2(days_of_supply),
        units_needed_until_delivery=units_needed,
        gap_before_delivery_units=gap_units,
        transfer_available_units=request.transfer_available_units,
        transfer_units_recommended=transfer_units,
        recommended_action=action,
        conclusion=conclusion,
    )


def summarize_outcome(outcome: DecisionOutcome) -> OutcomeSummary:
    sell_through_delta = outcome.actual_sell_through_units - outcome.predicted_sell_through_units
    waste_delta = outcome.actual_waste_units - outcome.predicted_waste_units
    total_error = abs(sell_through_delta) + abs(waste_delta)
    total_expected = max(
        outcome.predicted_sell_through_units + outcome.predicted_waste_units,
        1,
    )
    score = max(Decimal("0"), Decimal("1") - (_decimal(total_error) / _decimal(total_expected)))
    if sell_through_delta > 0:
        lesson = f"Learned {outcome.sku} sold faster than expected after {outcome.action}."
    elif waste_delta > 0:
        lesson = f"Learned {outcome.sku} wasted more than expected after {outcome.action}."
    else:
        lesson = f"Learned {outcome.sku} outcome matched the recommendation closely."
    return OutcomeSummary(
        sku=outcome.sku,
        action=outcome.action,
        sell_through_delta_units=sell_through_delta,
        waste_delta_units=waste_delta,
        score=_q2(score),
        lesson=lesson,
    )


def build_store_intelligence_demo() -> dict[str, Any]:
    as_of = date(2026, 7, 6)
    batch_split = split_stock_by_fefo(
        sku="4011",
        as_of=as_of,
        batches=(
            StockBatch(
                sku="4011",
                lot="AMASI-OLD-0707",
                units=10,
                expiry_date=date(2026, 7, 7),
                received_date=date(2026, 7, 3),
                location="fridge_a",
            ),
            StockBatch(
                sku="4011",
                lot="AMASI-NEW-0713",
                units=20,
                expiry_date=date(2026, 7, 13),
                received_date=date(2026, 7, 6),
                location="fridge_a",
            ),
        ),
    )
    delivery = reconcile_delivery(
        DeliveryReceipt(
            sku="4011",
            ordered_units=50,
            asn_units=50,
            received_units=38,
            accepted_units=32,
            short_dated_units=6,
        )
    )
    supplier_cover = plan_supplier_cover(
        SupplierCoverRequest(
            sku="4011",
            units_on_hand=12,
            forecast_daily_units=Decimal("10"),
            supplier_lead_time_days=Decimal("3"),
            transfer_available_units=18,
        )
    )
    learning = summarize_outcome(
        DecisionOutcome(
            sku="yoghurt_1l",
            action="markdown",
            predicted_sell_through_units=24,
            actual_sell_through_units=30,
            predicted_waste_units=8,
            actual_waste_units=5,
        )
    )
    return {
        "batch_split": batch_split.to_dict(),
        "delivery_reconciliation": delivery.to_dict(),
        "supplier_cover": supplier_cover.to_dict(),
        "learning_summary": learning.to_dict(),
    }
