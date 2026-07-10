"""Regression tests for the findings of the 20260710T004237Z stress-run audit."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def _drill_decision_ids(client: TestClient, seed: int) -> list[str]:
    response = client.get(
        "/demo/worldgen/stage4_payday_coldchain",
        params={"limit": 300, "assortment_size": 300, "seed_override": seed},
    )
    assert response.status_code == 200
    return [decision["id"] for decision in response.json()["decisions"]]


def test_decision_ids_are_unique_within_and_across_cycles() -> None:
    """Audit finding 1: 1,451 reused decision ids from 32-bit event-id collisions."""
    client = TestClient(app)
    first = _drill_decision_ids(client, seed=313)
    second = _drill_decision_ids(client, seed=314)

    assert first and second
    assert len(first) == len(set(first)), "decision ids collide within one cycle"
    assert len(second) == len(set(second))
    assert not set(first) & set(second), "decision ids collide across cycles/seeds"


def _expiry_event(event_id: str, days: int) -> dict[str, object]:
    return {
        "id": event_id,
        "type": "expiry_entry",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "wms_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "sku": "P00000042",
            "batch_id": "B01-P00000042",
            "category": "dairy",
            "storage": "chilled",
            "days_to_expiry": days,
        },
    }


def test_imminent_expiry_mints_inventory_review_and_routes_learning() -> None:
    """Audit findings 2+3: coverage collapsed to one domain; learning was a no-op."""
    client = TestClient(app)

    response = client.post("/ingest", json=_expiry_event("evt_expiry_audit_1", days=1))
    assert response.status_code == 200
    cascade = response.json()["cascade"]
    assert cascade["scenario"] == "expiry_risk_markdown_review"
    decision = cascade["decision"]
    assert decision["status"] == "pending"
    assert decision["role"] == "inventory_manager"

    approve = client.post(f"/decisions/{decision['id']}/approve")
    assert approve.status_code == 200
    event = approve.json()["learning_event"]
    assert event["metric"].endswith(":expiry_review_days_to_expiry")
    assert event["updated_threshold"] > 0, "learning must move state, not no-op"


def test_comfortable_expiry_stays_quiet() -> None:
    client = TestClient(app)
    response = client.post("/ingest", json=_expiry_event("evt_expiry_audit_2", days=6))
    assert response.status_code == 200
    assert response.json()["cascade"] is None


def test_price_exception_learning_uses_exposure_metric_with_varied_score() -> None:
    client = TestClient(app)
    sale = {
        "id": "evt_sale_audit_price_1",
        "type": "sale",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "pos_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "sku": "P00000077",
            "units": 2,
            "unit_price_cents": 1_000,
            "catalog_price_cents": 2_000,
        },
    }
    response = client.post("/ingest", json=sale)
    decision = response.json()["cascade"]["decision"]
    approve = client.post(f"/decisions/{decision['id']}/approve")

    assert approve.status_code == 200
    event = approve.json()["learning_event"]
    assert event["metric"] == "P00000077:price_exception_exposure_minor_units"
    assert event["updated_threshold"] >= 1_000
    assert event["outcome"]["success_score"] != "1.00", "score must discriminate outcomes"


def test_chat_answers_mention_the_asked_product() -> None:
    """Audit finding 4: every answer cited the same unrelated SKU."""
    client = TestClient(app)
    client.get("/demo/golden")

    response = client.post(
        "/chat", json={"question": "Why might Amasi 2L in Dairy need attention right now?"}
    )

    assert response.status_code == 200
    assert "Amasi" in response.text, response.text
