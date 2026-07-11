from __future__ import annotations

from decimal import Decimal

import pytest

from shelfwise_decision_science import (
    StockSourceCandidate,
    plan_stock_sourcing,
)


def _candidate(
    *,
    source_type: str = "branch",
    source_id: str = "store_07",
    available_units: int = 20,
    distance_km: str = "8",
    lead_time_hours: str = "2",
    unit_cost: str | None = None,
) -> StockSourceCandidate:
    return StockSourceCandidate(
        source_type=source_type,
        source_id=source_id,
        available_units=available_units,
        distance_km=Decimal(distance_km),
        lead_time_hours=Decimal(lead_time_hours),
        unit_cost=Decimal(unit_cost) if unit_cost is not None else None,
    )


def test_prefers_the_fastest_source_that_can_cover_the_shortage() -> None:
    branch = _candidate(
        source_type="branch", source_id="store_07", available_units=25,
        distance_km="8", lead_time_hours="2",
    )
    dc = _candidate(
        source_type="distribution_center", source_id="dc_gauteng", available_units=500,
        distance_km="120", lead_time_hours="26",
    )
    supplier = _candidate(
        source_type="supplier", source_id="supplier_gauteng_chilled_dairy",
        available_units=1000, distance_km="200", lead_time_hours="72",
    )

    plan = plan_stock_sourcing(
        sku="4011", units_needed=18, candidates=(branch, dc, supplier)
    )

    assert plan.selected_source_type == "branch"
    assert plan.selected_source_id == "store_07"
    assert plan.units_sourced == 18
    assert plan.remaining_gap_units == 0
    assert plan.recommended_action == "transfer_from_branch"
    assert "store_07" in plan.conclusion
    assert "2.00" in plan.conclusion
    assert "8.00" in plan.conclusion
    assert plan.ranked[0].source_id == "store_07"
    assert plan.eligible_considered == 3
    assert plan.candidates_considered == 3


def test_falls_back_to_distribution_center_when_no_branch_has_stock() -> None:
    branch = _candidate(
        source_type="branch", source_id="store_07", available_units=0,
        distance_km="8", lead_time_hours="2",
    )
    dc = _candidate(
        source_type="distribution_center", source_id="dc_gauteng", available_units=500,
        distance_km="120", lead_time_hours="26",
    )

    plan = plan_stock_sourcing(sku="4011", units_needed=18, candidates=(branch, dc))

    assert plan.selected_source_type == "distribution_center"
    assert plan.selected_source_id == "dc_gauteng"
    assert plan.recommended_action == "transfer_from_distribution_center"
    assert plan.eligible_considered == 1


def test_partial_cover_recommends_purchase_order_for_the_remainder() -> None:
    branch = _candidate(
        source_type="branch", source_id="store_07", available_units=5,
        distance_km="8", lead_time_hours="2",
    )

    plan = plan_stock_sourcing(sku="4011", units_needed=18, candidates=(branch,))

    assert plan.units_sourced == 5
    assert plan.remaining_gap_units == 13
    assert plan.recommended_action == "transfer_from_branch_and_purchase_order_remainder"
    assert "13" in plan.conclusion
    assert "purchase order" in plan.conclusion


def test_no_available_source_recommends_purchase_order_and_explains_why() -> None:
    branch = _candidate(source_type="branch", source_id="store_07", available_units=0)
    dc = _candidate(
        source_type="distribution_center", source_id="dc_gauteng", available_units=0
    )
    supplier = _candidate(
        source_type="supplier", source_id="supplier_gauteng_chilled_dairy", available_units=0
    )

    plan = plan_stock_sourcing(
        sku="4011", units_needed=18, candidates=(branch, dc, supplier)
    )

    assert plan.selected_source_type is None
    assert plan.selected_source_id is None
    assert plan.units_sourced == 0
    assert plan.remaining_gap_units == 18
    assert plan.recommended_action == "place_purchase_order"
    assert "none has any stock" in plan.conclusion
    assert "18" in plan.conclusion


def test_ties_on_lead_time_break_by_distance_then_cost() -> None:
    near = _candidate(
        source_type="branch", source_id="store_02", available_units=20,
        distance_km="5", lead_time_hours="4", unit_cost="10",
    )
    far = _candidate(
        source_type="branch", source_id="store_09", available_units=20,
        distance_km="40", lead_time_hours="4", unit_cost="8",
    )

    plan = plan_stock_sourcing(sku="4011", units_needed=10, candidates=(far, near))

    assert plan.selected_source_id == "store_02"
    assert "closer" in plan.conclusion


def test_rejects_non_positive_units_needed() -> None:
    with pytest.raises(ValueError, match="units_needed must be positive"):
        plan_stock_sourcing(sku="4011", units_needed=0, candidates=())


def test_rejects_unknown_source_type() -> None:
    with pytest.raises(ValueError, match="unknown source_type"):
        StockSourceCandidate(
            source_type="warehouse_moon_base",
            source_id="x",
            available_units=1,
            distance_km=Decimal("1"),
            lead_time_hours=Decimal("1"),
        )
