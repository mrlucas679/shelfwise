from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, candidate_store
from shelfwise_backend.candidate_factory import generate_fleet_candidates


def _seed_candidate(tenant_id: str = "sa_retail_demo"):
    candidate = generate_fleet_candidates(
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
    now = datetime(2026, 7, 13, tzinfo=UTC)
    candidate_store.upsert(candidate, now=now)
    candidate_store.suppress(
        tenant_id,
        candidate.candidate_key,
        reason="covered by open order",
        until=now + timedelta(days=1),
    )
    return candidate


def test_candidate_history_route_returns_ordered_transitions() -> None:
    candidate = _seed_candidate()
    client = TestClient(app)

    response = client.get(f"/candidates/{candidate.candidate_key}/history")

    assert response.status_code == 200
    body = response.json()
    assert body["candidate_key"] == candidate.candidate_key
    reasons = [entry["reason"] for entry in body["history"]]
    assert reasons == ["suppressed", "observed"]
    assert body["history"][0]["status"] == "suppressed"


def test_candidate_history_route_rejects_invalid_limit() -> None:
    candidate = _seed_candidate()
    client = TestClient(app)

    response = client.get(f"/candidates/{candidate.candidate_key}/history?limit=0")

    assert response.status_code == 422


def test_candidate_history_route_returns_empty_for_unknown_candidate() -> None:
    client = TestClient(app)

    response = client.get("/candidates/does-not-exist/history")

    assert response.status_code == 200
    assert response.json()["history"] == []
