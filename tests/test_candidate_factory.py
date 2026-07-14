from __future__ import annotations

from datetime import date

import pytest

from shelfwise_backend.candidate_factory import generate_fleet_candidates


def _item(sku: str, *, days: int, units: int, reasons: list[str]) -> dict[str, object]:
    return {
        "sku": sku,
        "name": f"Product {sku}",
        "category": "Dairy",
        "supplier": "Supplier",
        "on_hand": units,
        "reorder_point": 20,
        "days_to_expiry": days,
        "attention_reasons": reasons,
        "batches": [
            {
                "lot_id": f"LOT-{sku}",
                "on_hand": units,
                "days_to_expiry": days,
            }
        ],
    }


def test_candidates_are_stable_bounded_and_ranked_without_model_calls() -> None:
    items = [
        _item("low", days=30, units=20, reasons=["low_stock"]),
        _item("urgent", days=0, units=100, reasons=["expiring"]),
    ]

    first = generate_fleet_candidates(
        items, tenant_id="tenant-a", as_of=date(2026, 7, 13), limit=10
    )
    second = generate_fleet_candidates(
        items, tenant_id="tenant-a", as_of=date(2026, 7, 13), limit=10
    )

    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert first[0].sku == "urgent"
    assert {item.candidate_type for item in first} == {"expiry_risk", "low_stock"}
    assert first[-1].monitoring_only is True


def test_expired_lots_are_separate_from_expiry_risk_and_include_evidence() -> None:
    candidates = generate_fleet_candidates(
        [_item("milk", days=-2, units=7, reasons=["blocked"])],
        tenant_id="tenant-a",
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_type == "expired_lot"
    assert candidates[0].lot_id == "LOT-milk"
    assert candidates[0].exposure_units == 7
    assert candidates[0].evidence["days_to_expiry"] == -2


def test_candidate_input_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        generate_fleet_candidates([], tenant_id=" ")
    with pytest.raises(ValueError, match="limit"):
        generate_fleet_candidates([], tenant_id="tenant-a", limit=501)


def test_optional_operational_signals_create_typed_candidates() -> None:
    candidates = generate_fleet_candidates(
        [
            {
                "sku": "SKU-SIGNALS",
                "category": "Dairy",
                "on_hand": 80,
                "reorder_point": 10,
                "days_to_expiry": 30,
                "has_batch_evidence": False,
                "supplier_recent_delay": True,
                "recent_daily_units": [1, 1, 1],
                "cold_chain_risk": True,
            }
        ],
        tenant_id="tenant-a",
    )

    assert {item.candidate_type for item in candidates} == {
        "supplier_delay",
        "slow_mover",
        "missing_batch_expiry",
        "cold_chain_risk",
    }
    assert all(item.evidence["product_policy"] == "dairy_chilled_v1" for item in candidates)
