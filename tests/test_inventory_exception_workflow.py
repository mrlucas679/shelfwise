from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def _event(exception_type: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "exception_id": f"EXC-{exception_type}",
        "exception_type": exception_type,
        "sku": "4011",
        "reason": f"test {exception_type}",
        "location": "store_12_soweto",
        **extra,
    }
    return {
        "id": f"evt_exception_{exception_type}",
        "type": "inventory_exception",
        "ts": "2026-07-11T09:00:00Z",
        "actor": "inventory_team",
        "source": "manual",
        "tenant_id": "sa_retail_demo",
        "payload": payload,
    }


@pytest.mark.parametrize(
    ("event", "action_type", "units"),
    [
        (_event("return", units=3, source_reference="RETURN-001"), "process_return", 3),
        (
            _event("damage", units=4, source_reference="DAMAGE-001"),
            "quarantine_damaged_stock",
            4,
        ),
        (
            _event("shrink", expected_units=30, counted_units=24, count_reference="COUNT-001"),
            "investigate_shrink",
            6,
        ),
        (
            _event(
                "misplaced_stock",
                units=5,
                expected_location="shelf_a",
                observed_location="backroom_b",
            ),
            "relocate_stock",
            5,
        ),
    ],
)
def test_inventory_exception_types_route_distinct_governed_actions(
    event: dict[str, object], action_type: str, units: int
) -> None:
    response = TestClient(app).post("/ingest", json=event)

    assert response.status_code == 200
    decision = response.json()["cascade"]["decision"]
    assert decision["status"] == "pending"
    assert decision["action"]["type"] == action_type
    assert decision["action"]["params"]["units"] == units
    assert decision["expected_outcome"]["units_reconciled"] == units
    assert decision["role"] == "inventory_manager"


@pytest.mark.parametrize(
    "event, detail",
    [
        (
            _event("shrink", expected_units=20, counted_units=20, count_reference="COUNT-BAD"),
            "shrink requires expected_units > counted_units >= 0",
        ),
        (
            _event(
                "misplaced_stock",
                units=2,
                expected_location="shelf_a",
                observed_location="shelf_a",
            ),
            "misplaced_stock locations must differ",
        ),
        (_event("damage", units=2), "damage requires source_reference"),
        (_event("unknown", units=2), "inventory_exception exception_type must be one of"),
    ],
)
def test_invalid_inventory_exceptions_fail_before_persistence(
    event: dict[str, object], detail: str
) -> None:
    client = TestClient(app)

    response = client.post("/ingest", json=event)
    events = client.get("/events").json()["events"]

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert all(item["id"] != event["id"] for item in events)


def test_inventory_exception_demo_registers_trace_and_approval_task() -> None:
    client = TestClient(app)

    response = client.post("/demo/inventory-exception")
    body = response.json()
    decision_id = body["decision"]["id"]
    trace = client.get(f"/trace/{body['correlation_id']}")
    approved = client.post(f"/decisions/{decision_id}/approve")
    tasks = client.get("/writeback/tasks").json()["tasks"]

    assert response.status_code == 200
    assert body["scenario"] == "inventory_exception_review"
    assert body["decision"]["action"]["type"] == "investigate_shrink"
    assert trace.status_code == 200
    assert approved.json()["decision"]["status"] == "approved"
    task = next(item for item in tasks if item["idempotency_key"] == f"writeback:{decision_id}")
    assert task["action"]["type"] == "investigate_shrink"

    receipt = {
        "source_reference": "COUNT-COMPLETE-001",
        "completed_units": task["action"]["params"]["units"],
        "observed_location": "store_12_soweto",
        "note": "Cycle count investigation completed",
    }
    completed = client.post(f"/writeback/tasks/{task['id']}/complete", json=receipt)
    replayed = client.post(f"/writeback/tasks/{task['id']}/complete", json=receipt)

    assert completed.status_code == 200
    assert completed.json()["task"]["status"] == "completed"
    assert completed.json()["task"]["completion_receipt"]["source_reference"] == receipt[
        "source_reference"
    ]
    assert replayed.json()["task"] == completed.json()["task"]


def test_completed_relocation_updates_physical_position_ledger() -> None:
    client = TestClient(app)
    event = _event(
        "misplaced_stock",
        units=5,
        expected_location="shelf_a",
        observed_location="backroom_b",
    )
    decision = client.post("/ingest", json=event).json()["cascade"]["decision"]
    approved = client.post(f"/decisions/{decision['id']}/approve").json()
    task = approved["decision"]["write_back"]

    completed = client.post(
        f"/writeback/tasks/{task['id']}/complete",
        json={
            "source_reference": "MOVE-4011-001",
            "completed_units": 5,
            "observed_location": "shelf_a",
        },
    )
    positions = client.get("/inventory/positions?sku=4011").json()["positions"]

    assert completed.status_code == 200
    assert {(item["location_id"], item["quantity"], item["state"]) for item in positions} == {
        ("backroom_b", 0, "relocated"),
        ("shelf_a", 5, "available"),
    }
    mismatch = client.post(
        f"/writeback/tasks/{task['id']}/complete",
        json={"source_reference": "MOVE-OTHER", "completed_units": 4},
    )
    assert mismatch.status_code == 409
