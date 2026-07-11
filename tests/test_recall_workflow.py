from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def _recall_event(*, event_id: str = "evt_recall_001") -> dict[str, object]:
    return {
        "id": event_id,
        "type": "recall_notice",
        "ts": "2026-07-11T09:00:00Z",
        "actor": "supplier_dairyco",
        "source": "api",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "recall_id": "REC-2026-0711",
            "sku": "4011",
            "lot_id": "AMASI-OLD-0707",
            "units": 10,
            "location": "store_12_soweto",
            "reason": "possible cold-chain contamination",
            "issued_by": "DairyCo Quality",
        },
    }


def test_recall_notice_creates_sourced_pending_quarantine_decision() -> None:
    client = TestClient(app)

    response = client.post("/ingest", json=_recall_event())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    cascade = body["cascade"]
    decision = cascade["decision"]
    assert decision["status"] == "pending"
    assert decision["action"]["type"] == "quarantine_lot"
    assert decision["action"]["risk_tier"] == "high"
    assert decision["action"]["params"]["stop_sale"] is True
    assert decision["action"]["params"]["issuer_verified"] is False
    assert decision["action"]["params"]["lot_id"] == "AMASI-OLD-0707"
    assert decision["expected_outcome"]["units_quarantined"] == 10
    assert decision["caused_by"] == ["evt_recall_001"]
    assert [item["agent"] for item in cascade["evidence"]] == [
        "inventory",
        "critic",
        "executive",
    ]
    assert all(item["sources"] for item in cascade["evidence"])


def test_invalid_recall_is_rejected_before_event_persistence() -> None:
    client = TestClient(app)
    payload = _recall_event(event_id="evt_recall_invalid")
    payload["payload"] = {"recall_id": "REC-INCOMPLETE", "units": 0}

    response = client.post("/ingest", json=payload)
    events = client.get("/events").json()["events"]

    assert response.status_code == 422
    assert "missing fields" in response.json()["detail"]
    assert all(item["id"] != "evt_recall_invalid" for item in events)


def test_recall_rejects_oversized_untrusted_identifiers() -> None:
    client = TestClient(app)
    payload = _recall_event(event_id="evt_recall_oversized")
    payload["payload"]["recall_id"] = "R" * 201

    response = client.post("/ingest", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == "recall_notice recall_id exceeds 200 characters"


def test_recall_retry_is_idempotent_and_approval_creates_writeback_task() -> None:
    client = TestClient(app)
    payload = _recall_event(event_id="evt_recall_retry")

    first = client.post("/ingest", json=payload)
    duplicate = client.post("/ingest", json=payload)
    decision_id = first.json()["cascade"]["decision"]["id"]
    approved = client.post(f"/decisions/{decision_id}/approve")
    tasks = client.get("/writeback/tasks").json()["tasks"]

    assert first.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["cascade"] is None
    assert approved.status_code == 200
    assert approved.json()["decision"]["status"] == "approved"
    task = next(item for item in tasks if item["idempotency_key"] == f"writeback:{decision_id}")
    assert task["status"] == "pending_external_write"
    assert task["action"]["type"] == "quarantine_lot"


def test_recall_demo_uses_event_pipeline_and_registers_trace() -> None:
    client = TestClient(app)

    response = client.post("/demo/recall")
    body = response.json()
    trace = client.get(f"/trace/{body['correlation_id']}")
    events = client.get("/events").json()["events"]

    assert response.status_code == 200
    assert body["scenario"] == "supplier_lot_recall_quarantine"
    assert body["decision"]["action"]["type"] == "quarantine_lot"
    assert body["decision"]["action"]["params"]["issuer_verified"] is True
    assert trace.status_code == 200
    assert any(item["id"] in body["decision"]["caused_by"] for item in events)
