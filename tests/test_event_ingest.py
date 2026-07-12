from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.app import app
from shelfwise_contracts import Event

_HERO_SKU = app_module.world_facts.get_hero_sku("sa_retail_demo")
_HERO_SUPPLIER = app_module.world_facts.get_supplier_for_sku("sa_retail_demo", _HERO_SKU)["name"]
_HERO_UNIT_PRICE = str(
    app_module.world_facts.get_scenario_facts("sa_retail_demo", _HERO_SKU).unit_price.amount
)


def _scan_event(event_id: str = "evt_scan_hero") -> dict[str, object]:
    return {
        "id": event_id,
        "type": "scan",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "scanner",
        "tenant_id": "sa_retail_demo",
        "payload": {"sku": _HERO_SKU, "location": "store_12"},
    }


def _supplier_event(event_id: str = "evt_supplier_hero") -> dict[str, object]:
    return {
        "id": event_id,
        "type": "supplier_update",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "procurement",
        "source": "manual",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "supplier": _HERO_SUPPLIER,
            "avg_lead_time_days": "3",
            "recent_delay": False,
        },
    }


def _sale_event(event_id: str = "evt_sale_hero") -> dict[str, object]:
    return {
        "id": event_id,
        "type": "sale",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "pos_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "sku": _HERO_SKU,
            "location": "store_12",
            "quantity": 2,
            "unit_price": _HERO_UNIT_PRICE,
        },
    }


def _cold_chain_event(event_id: str = "evt_cold_chain_fridge_dairy_1") -> dict[str, object]:
    return {
        "id": event_id,
        "type": "cold_chain_alert",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "api",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "site_id": "store_12",
            "asset_id": "fridge_dairy_1",
            "category": "dairy",
            "diagnosis": "generator_failed",
            "severity": 2,
            "predicted_minutes_to_unsafe": "18",
            "measured_outage_hours": "4",
            "stock_at_risk": {"minor_units": 643500, "currency": "ZAR"},
        },
    }


def test_event_contract_is_traceable_and_tolerant() -> None:
    wire = _scan_event()
    wire["future_field"] = "ignored"

    event = Event.parse_wire(wire)
    cloud = event.to_cloudevent()

    assert event.correlation_id == event.id
    assert event.tenant_id == "sa_retail_demo"
    assert event.payload["sku"] == _HERO_SKU
    assert "future_field" not in event.to_dict()
    assert cloud["type"] == "shelfwise.scan"
    assert cloud["tenantid"] == "sa_retail_demo"
    assert cloud["correlationid"] == event.id


def test_ingest_records_event_runs_supported_scan_once() -> None:
    client = TestClient(app)

    first = client.post("/ingest", json=_scan_event())
    duplicate = client.post("/ingest", json=_scan_event())
    events = client.get("/events")
    bus = client.get("/events/bus")

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "accepted"
    assert first_body["bus_message_id"] == "mem-1"
    assert first_body["cascade"]["decision"]["action"]["type"] == "apply_markdown"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["bus_message_id"] is None
    assert duplicate.json()["cascade"] is None
    assert events.status_code == 200
    assert [event["id"] for event in events.json()["events"]] == ["evt_scan_hero"]
    assert bus.status_code == 200
    assert [message["event"]["id"] for message in bus.json()["messages"]] == ["evt_scan_hero"]


def test_ingest_self_heals_when_bus_publish_fails_after_event_is_recorded(monkeypatch) -> None:
    """A recorded-but-never-published event must not be swallowed as a duplicate on retry."""
    client = TestClient(app)
    original_publish = app_module.event_bus.publish
    calls = {"n": 0}

    def flaky_publish(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bus unavailable")
        return original_publish(event)

    monkeypatch.setattr(app_module.event_bus, "publish", flaky_publish)
    event = _scan_event("evt_selfheal_4011")

    with pytest.raises(RuntimeError, match="bus unavailable"):
        client.post("/ingest", json=event)

    retry = client.post("/ingest", json=event)

    assert retry.status_code == 200
    assert retry.json()["status"] == "accepted"
    assert retry.json()["bus_message_id"] is not None
    assert calls["n"] == 2


def test_ingest_supplier_update_runs_procurement_cascade() -> None:
    client = TestClient(app)

    response = client.post("/ingest", json=_supplier_event())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["cascade"]["scenario"] == "procurement_reorder_supplier_cover"
    assert body["cascade"]["decision"]["role"] == "procurement_manager"
    assert body["cascade"]["decision"]["action"]["type"] == "reorder"


def test_ingest_sale_runs_sales_cascade() -> None:
    client = TestClient(app)

    response = client.post("/ingest", json=_sale_event())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["cascade"]["scenario"] == "pos_sale_price_integrity"
    assert body["cascade"]["decision"]["role"] == "sales_manager"
    assert body["cascade"]["decision"]["action"]["type"] == "record_sale"


def _catalog_sale_event(
    event_id: str,
    *,
    sku: str,
    unit_price_cents: int,
    catalog_price_cents: int,
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": "sale",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "pos_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "sku": sku,
            "units": 3,
            "unit_price_cents": unit_price_cents,
            "catalog_price_cents": catalog_price_cents,
        },
    }


def test_ingest_in_band_catalog_sale_records_without_minting_a_decision() -> None:
    client = TestClient(app)

    response = client.post(
        "/ingest",
        json=_catalog_sale_event(
            "evt_sale_inband_p1",
            sku="P00000042",
            unit_price_cents=2_090,
            catalog_price_cents=1_999,
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["cascade"] is None


def test_ingest_outlier_catalog_sale_mints_pending_price_exception() -> None:
    client = TestClient(app)

    response = client.post(
        "/ingest",
        json=_catalog_sale_event(
            "evt_sale_outlier_p2",
            sku="P00000077",
            unit_price_cents=1_099,
            catalog_price_cents=1_999,
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    cascade = body["cascade"]
    assert cascade["scenario"] == "pos_price_outlier_review"
    decision = cascade["decision"]
    assert decision["status"] == "pending"
    assert decision["role"] == "sales_manager"
    assert decision["action"]["type"] == "review_price_exception"
    assert decision["action"]["params"]["sku"] == "P00000077"

    listed = client.get(f"/decisions/{decision['id']}")
    assert listed.status_code == 200
    assert listed.json()["decision"]["status"] == "pending"


def test_ingest_catalog_sale_without_reference_price_stays_quiet() -> None:
    client = TestClient(app)

    event = _catalog_sale_event(
        "evt_sale_no_reference_p3",
        sku="P00000088",
        unit_price_cents=1_099,
        catalog_price_cents=1_999,
    )
    del event["payload"]["catalog_price_cents"]

    response = client.post("/ingest", json=event)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["cascade"] is None


def test_ingest_cold_chain_alert_runs_facilities_cascade() -> None:
    client = TestClient(app)

    response = client.post("/ingest", json=_cold_chain_event())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["cascade"]["scenario"] == "cold_chain_generator_failure_facilities_review"
    assert body["cascade"]["decision"]["role"] == "facilities_manager"
    assert body["cascade"]["decision"]["action"]["type"] == "dispatch_facilities_check"
    assert body["cascade"]["decision"]["caused_by"] == ["evt_cold_chain_fridge_dairy_1"]


def test_ingest_queues_without_inline_cascade_when_worker_enabled(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_ENABLED", "true")
    client = TestClient(app)

    response = client.post("/ingest", json=_scan_event("evt_worker_enabled_queue"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["bus_message_id"] == "mem-1"
    assert body["cascade"] is None


def test_ingest_rejects_malformed_event() -> None:
    client = TestClient(app)

    response = client.post("/ingest", json={})

    assert response.status_code == 422
    assert "event missing fields" in response.json()["detail"]


def test_write_path_api_key_guards_ingest_and_approval(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("API_KEY", "secret")

    blocked_ingest = client.post("/ingest", json=_scan_event("evt_guarded"))
    allowed_ingest = client.post(
        "/ingest",
        json=_scan_event("evt_guarded"),
        headers={"x-api-key": "secret"},
    )
    decision_id = allowed_ingest.json()["cascade"]["decision"]["id"]
    blocked_approve = client.post(f"/decisions/{decision_id}/approve")
    allowed_approve = client.post(
        f"/decisions/{decision_id}/approve",
        headers={"x-api-key": "secret"},
    )

    assert blocked_ingest.status_code == 401
    assert allowed_ingest.status_code == 200
    assert blocked_approve.status_code == 401
    assert allowed_approve.status_code == 200
    assert allowed_approve.json()["decision"]["status"] == "approved"
