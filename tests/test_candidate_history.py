from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shelfwise_backend.candidate_factory import generate_fleet_candidates
from shelfwise_backend.candidate_history import InMemoryCandidateHistoryStore
from shelfwise_backend.candidate_store import InMemoryCandidateStore


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


def test_first_observation_records_one_history_entry() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()

    store.upsert(candidate, now=datetime(2026, 7, 13, tzinfo=UTC))

    entries = store.history("tenant-a", candidate.candidate_key)
    assert len(entries) == 1
    assert entries[0].reason == "observed"
    assert entries[0].sequence == 1


def test_reobserving_with_no_status_change_does_not_grow_history() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()
    now = datetime(2026, 7, 13, tzinfo=UTC)

    store.upsert(candidate, now=now)
    store.upsert(candidate, now=now + timedelta(hours=1))
    store.upsert(candidate, now=now + timedelta(hours=2))

    entries = store.history("tenant-a", candidate.candidate_key)
    assert len(entries) == 1


def test_suppress_and_link_decision_each_append_one_ordered_entry() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()
    now = datetime(2026, 7, 13, tzinfo=UTC)
    store.upsert(candidate, now=now)

    store.suppress(
        "tenant-a",
        candidate.candidate_key,
        reason="open purchase order covers the gap",
        until=now + timedelta(days=1),
    )
    refreshed = store.upsert(candidate, now=now + timedelta(days=2))
    assert refreshed["status"] == "monitoring"
    store.link_decision("tenant-a", candidate.candidate_key, "dec-1")

    entries = store.history("tenant-a", candidate.candidate_key)
    reasons = [entry.reason for entry in entries]
    # newest first
    assert reasons == ["linked_decision", "status_changed", "suppressed", "observed"]
    assert [entry.sequence for entry in entries] == [4, 3, 2, 1]
    assert entries[0].status == "pending"
    assert entries[0].decision_id == "dec-1"


def test_history_is_tenant_isolated() -> None:
    store = InMemoryCandidateStore()
    candidate_a = _candidate("tenant-a")
    candidate_b = _candidate("tenant-b")
    store.upsert(candidate_a)
    store.upsert(candidate_b)

    assert len(store.history("tenant-a", candidate_a.candidate_key)) == 1
    assert store.history("tenant-b", candidate_a.candidate_key) == []
    assert len(store.history("tenant-b", candidate_b.candidate_key)) == 1


def test_history_since_and_until_filter_by_recorded_time() -> None:
    history = InMemoryCandidateHistoryStore()
    record = {
        "tenant_id": "tenant-a",
        "data_domain": "world_simulation",
        "candidate_key": "cand-1",
        "status": "new",
        "score": 1.0,
        "urgency": 1.0,
        "exposure_units": 1,
        "decision_id": None,
    }
    history.record(record, reason="observed")
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    history.record({**record, "status": "monitoring"}, reason="status_changed")

    before_cutoff = history.list("tenant-a", "cand-1", until=cutoff)
    after_cutoff = history.list(
        "tenant-a", "cand-1", since=cutoff + timedelta(seconds=2)
    )
    assert len(before_cutoff) >= 1
    assert after_cutoff == []


def test_history_limit_is_validated() -> None:
    history = InMemoryCandidateHistoryStore()
    with pytest.raises(ValueError):
        history.list("tenant-a", "cand-1", limit=0)
    with pytest.raises(ValueError):
        history.list("tenant-a", "cand-1", limit=10_000)


def test_clear_also_clears_history() -> None:
    store = InMemoryCandidateStore()
    candidate = _candidate()
    store.upsert(candidate)
    assert store.history("tenant-a", candidate.candidate_key) != []

    store.clear()

    assert store.history("tenant-a", candidate.candidate_key) == []
