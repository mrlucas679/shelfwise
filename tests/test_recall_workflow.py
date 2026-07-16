from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.state import cascade_worker


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
    tasks = client.get(
        "/writeback/tasks?data_domain=operational_twin"
    ).json()["tasks"]

    assert first.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["cascade"] is None
    assert approved.status_code == 200
    assert approved.json()["decision"]["status"] == "approved"
    task = next(item for item in tasks if item["idempotency_key"] == f"writeback:{decision_id}")
    assert task["status"] == "pending_external_write"
    assert task["action"]["type"] == "quarantine_lot"


def test_recall_demo_waits_for_the_real_async_worker_instead_of_failing(monkeypatch) -> None:
    """Real production topology (`WORKER_ENABLED=true`) defers cascade computation to the
    async worker queue - `_record_pipeline_event` intentionally returns `cascade: None`,
    exactly as it would for a genuine recall arriving from a source system. Before this fix,
    `/scenarios/recall` treated that correct async behavior as a hard 500 (a leftover single-
    process demo assumption). The route must instead wait for the worker - which really does
    process the event on its own thread here - and return the decision it actually produces.
    """
    monkeypatch.setenv("WORKER_ENABLED", "true")
    client = TestClient(app)
    stop = threading.Event()

    def drive_worker() -> None:
        while not stop.is_set():
            cascade_worker.process_one()
            time.sleep(0.02)

    worker_thread = threading.Thread(target=drive_worker, daemon=True)
    worker_thread.start()
    try:
        response = client.post("/scenarios/recall")
    finally:
        stop.set()
        worker_thread.join(timeout=5)

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["action"]["type"] == "quarantine_lot"
    assert decision["status"] == "pending"


def test_recall_demo_resubmission_looks_up_the_existing_async_decision(monkeypatch) -> None:
    """A repeat drill click resubmits the same deterministic event id. Under the real async
    worker, the second submission's own `_record_pipeline_event` call returns
    `status: "duplicate", cascade: None` (the event was already published once) - the route
    must look up the decision the worker already produced for the first submission, not
    treat a legitimate idempotent resubmission as a timeout or a failure.
    """
    monkeypatch.setenv("WORKER_ENABLED", "true")
    client = TestClient(app)
    scope = "resubmission-probe"
    stop = threading.Event()

    def drive_worker() -> None:
        while not stop.is_set():
            cascade_worker.process_one()
            time.sleep(0.02)

    worker_thread = threading.Thread(target=drive_worker, daemon=True)
    worker_thread.start()
    try:
        first = client.post("/scenarios/recall", params={"run_scope": scope})
    finally:
        stop.set()
        worker_thread.join(timeout=5)

    assert first.status_code == 200
    first_decision_id = first.json()["decision"]["id"]

    # No worker thread running for the resubmission - the lookup must be synchronous,
    # found from what the worker already persisted, not another wait-for-worker cycle.
    second = client.post("/scenarios/recall", params={"run_scope": scope})

    assert second.status_code == 200
    assert second.json()["decision"]["id"] == first_decision_id


def test_recall_demo_returns_a_truthful_still_processing_signal_not_a_fake_500(
    monkeypatch,
) -> None:
    """If the worker genuinely has not produced a decision within the wait bound (queue
    backed up, worker briefly down), the route must say so honestly (503, still processing)
    instead of the old blanket "did not produce a decision" 500 that implied a hard failure
    for what was actually just a slow but successful submission.
    """
    from shelfwise_backend import app as app_module

    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_SCENARIO_DRILL_WAIT_SECONDS", "0.3")
    monkeypatch.setattr(app_module, "_DEMO_DRILL_POLL_S", 0.05)
    client = TestClient(app)

    response = client.post("/scenarios/recall")

    assert response.status_code == 503
    assert "queued" in response.json()["detail"]


def test_recall_demo_uses_event_pipeline_and_registers_trace() -> None:
    client = TestClient(app)

    response = client.post("/scenarios/recall")
    body = response.json()
    trace = client.get(f"/trace/{body['correlation_id']}")
    events = client.get("/events").json()["events"]

    assert response.status_code == 200
    assert body["scenario"] == "supplier_lot_recall_quarantine"
    assert body["decision"]["action"]["type"] == "quarantine_lot"
    assert body["decision"]["action"]["params"]["issuer_verified"] is True
    assert trace.status_code == 200
    assert any(item["id"] in body["decision"]["caused_by"] for item in events)
