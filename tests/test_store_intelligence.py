from __future__ import annotations

from datetime import date
from decimal import Decimal

from shelfwise_data import (
    DecisionOutcome,
    DeliveryReceipt,
    StockBatch,
    SupplierCoverRequest,
    plan_supplier_cover,
    reconcile_delivery,
    split_stock_by_fefo,
    summarize_outcome,
)


def test_old_new_milk_batch_masking_splits_priority_and_normal_stock() -> None:
    split = split_stock_by_fefo(
        sku="milk_2l",
        as_of=date(2026, 7, 6),
        batches=(
            StockBatch(
                sku="milk_2l",
                lot="MILK-OLD-0707",
                units=10,
                expiry_date=date(2026, 7, 7),
                received_date=date(2026, 7, 3),
                location="fridge_a",
            ),
            StockBatch(
                sku="milk_2l",
                lot="MILK-NEW-0713",
                units=20,
                expiry_date=date(2026, 7, 13),
                received_date=date(2026, 7, 6),
                location="fridge_a",
            ),
        ),
    )

    assert split.total_units == 30
    assert split.priority_sell_units == 10
    assert split.normal_units == 20
    assert split.blocked_units == 0
    assert [batch.lot for batch in split.fefo_batches] == ["MILK-OLD-0707", "MILK-NEW-0713"]
    assert split.fefo_batches[0].stock_status == "priority_sell"
    assert split.fefo_batches[1].stock_status == "normal"


def test_delivery_ordered_vs_received_mismatch_flags_missing_units() -> None:
    reconciliation = reconcile_delivery(
        DeliveryReceipt(
            sku="milk_2l",
            ordered_units=50,
            asn_units=50,
            received_units=38,
            accepted_units=32,
            short_dated_units=6,
        )
    )

    assert reconciliation.status == "exception"
    assert reconciliation.missing_units == 12
    assert reconciliation.over_units == 0
    assert reconciliation.short_dated_units == 6
    assert reconciliation.supplier_fill_rate == Decimal("0.76")


def test_supplier_lead_time_prefers_transfer_or_hold_over_ordering_too_late() -> None:
    late_supplier = plan_supplier_cover(
        SupplierCoverRequest(
            sku="milk_2l",
            units_on_hand=12,
            forecast_daily_units=Decimal("10"),
            supplier_lead_time_days=Decimal("3"),
            transfer_available_units=18,
        )
    )
    enough_cover = plan_supplier_cover(
        SupplierCoverRequest(
            sku="milk_2l",
            units_on_hand=40,
            forecast_daily_units=Decimal("10"),
            supplier_lead_time_days=Decimal("3"),
            transfer_available_units=18,
        )
    )

    assert late_supplier.recommended_action == "transfer"
    assert late_supplier.gap_before_delivery_units == 18
    assert late_supplier.transfer_units_recommended == 18
    assert late_supplier.days_of_supply == Decimal("1.20")
    assert enough_cover.recommended_action == "hold"
    assert enough_cover.gap_before_delivery_units == 0


def test_outcome_summary_turns_result_into_learning_signal() -> None:
    summary = summarize_outcome(
        DecisionOutcome(
            sku="yoghurt_1l",
            action="markdown",
            predicted_sell_through_units=24,
            actual_sell_through_units=30,
            predicted_waste_units=8,
            actual_waste_units=5,
        )
    )

    assert summary.sell_through_delta_units == 6
    assert summary.waste_delta_units == -3
    assert summary.score == Decimal("0.72")
    assert "sold faster" in summary.lesson
