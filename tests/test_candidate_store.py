from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shelfwise_backend.candidate_factory import generate_fleet_candidates
from shelfwise_backend.candidate_store import InMemoryCandidateStore
from shelfwise_backend.product_catalog import _suppress_covered_candidates


def _candidate(tenant_id: str = "tenant-a"):
    return generate_fleet_candidates(
        [
            {
                "sku": "SKU-1",
                "name": "Milk",
                "category": "Dairy",
                "supplier": "Supplier",
                "on_hand": 20,
                "reorder_point": 20,
                "days_to_expiry": 30,
                "attention_reasons": ["low_stock"],
                "batches": [],
            }
        ],
        tenant_id=tenant_id,
    )[0]


def test_candidate_store_is_idempotent_and_tenant_scoped() -> None:
    store = InMemoryCandidateStore()
    now = datetime(2026, 7, 13, tzinfo=UTC)
    candidate = _candidate()

    first = store.upsert(candidate, now=now)
    second = store.upsert(candidate, now=now)

    assert first["candidate_key"] == second["candidate_key"]
    assert len(store.list("tenant-a")) == 1
    assert store.list("tenant-b") == []


def test_suppression_expires_and_does_not_reopen_terminal_candidate() -> None:
    store = InMemoryCandidateStore()
    now = datetime(2026, 7, 13, tzinfo=UTC)
    candidate = _candidate()
    store.upsert(candidate, now=now)

    suppressed = store.suppress(
        "tenant-a",
        candidate.candidate_key,
        reason="open purchase order covers the gap",
        until=now + timedelta(days=1),
    )
    assert suppressed is not None
    assert suppressed["status"] == "suppressed"
    assert suppressed["suppression_reason"] == "open purchase order covers the gap"

    refreshed = store.upsert(candidate, now=now + timedelta(days=2))
    assert refreshed["status"] == "monitoring"
    assert refreshed["suppressed_until"] is None

    store.link_decision("tenant-a", candidate.candidate_key, "dec-1")
    store.suppress(
        "tenant-a",
        candidate.candidate_key,
        reason="late duplicate",
        until=now + timedelta(days=3),
    )
    assert store.get("tenant-a", candidate.candidate_key)["status"] == "pending"


def test_decision_link_is_non_empty_idempotent_and_does_not_replace_existing_link() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()
    store.upsert(candidate)

    with pytest.raises(ValueError, match="decision_id"):
        store.link_decision("tenant-a", candidate.candidate_key, " ")

    first = store.link_decision("tenant-a", candidate.candidate_key, "dec-1")
    second = store.link_decision("tenant-a", candidate.candidate_key, "dec-2")

    assert first is not None
    assert second is not None
    assert second["decision_id"] == "dec-1"
    assert second["status"] == "pending"


def test_decision_link_does_not_reopen_terminal_candidate() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()
    store.upsert(candidate)
    store._records[("tenant-a", candidate.candidate_key)]["status"] = "approved"

    linked = store.link_decision("tenant-a", candidate.candidate_key, "dec-1")

    assert linked is not None
    assert linked["status"] == "approved"
    assert linked["decision_id"] is None


def test_open_order_coverage_suppresses_low_stock_candidate_with_receipt() -> None:
    store = InMemoryCandidateStore()
    candidate = generate_fleet_candidates(
        [
            {
                "sku": "SKU-1",
                "name": "Milk",
                "category": "Dairy",
                "supplier": "Supplier",
                "on_hand": 4,
                "reorder_point": 20,
                "days_to_expiry": 30,
                "attention_reasons": ["low_stock"],
                "batches": [],
            }
        ],
        tenant_id="tenant-a",
    )
    store.upsert(candidate[0])

    result = _suppress_covered_candidates(
        store.list("tenant-a"),
        candidate_store=store,
        open_orders={"SKU-1": {"remaining_units": 20, "eta": "2026-07-15T10:00:00+00:00"}},
        now=datetime(2026, 7, 13, tzinfo=UTC),
        tenant_id="tenant-a",
    )

    assert result[0]["status"] == "suppressed"
    assert "open order covers" in result[0]["suppression_reason"]
