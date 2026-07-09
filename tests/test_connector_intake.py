from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def _square_payload(quantity: str = "240") -> dict[str, object]:
    return {
        "type": "inventory.count.updated",
        "data": {
            "object": {
                "inventory_counts": [
                    {
                        "catalog_object_id": "sq_4011",
                        "location_id": "store_12",
                        "quantity": quantity,
                    }
                ]
            }
        },
    }


def _shopify_payload() -> dict[str, object]:
    return {
        "id": 1234,
        "created_at": "2026-07-06T10:00:00Z",
        "location_id": "store_12",
        "line_items": [{"id": 1, "sku": "4011", "quantity": 2, "price": "30.00"}],
    }


def test_square_connector_intake_stores_inbound_record_and_event_once() -> None:
    client = TestClient(app)
    payload = _square_payload()

    first = client.post("/connectors/square/intake", json={"payload": payload})
    duplicate = client.post("/connectors/square/intake", json={"payload": payload})
    records = client.get("/connectors/inbound-records")
    bus = client.get("/events/bus")

    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "accepted"
    assert body["record"]["source_system"] == "square"
    assert body["record"]["raw_payload_hash"]
    assert body["record"]["has_raw_payload"] is True
    assert "raw_payload" not in body["record"]
    assert body["event"]["type"] == "stock_update"
    assert body["event"]["payload"]["sku"] == "sq_4011"
    assert body["pipeline"]["bus_message_id"] == "mem-1"
    assert body["pipeline"]["cascade"] is None

    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert records.status_code == 200
    assert len(records.json()["records"]) == 1
    assert bus.status_code == 200
    assert [message["event"]["id"] for message in bus.json()["messages"]] == [body["event"]["id"]]


def test_shopify_connector_intake_enters_sales_cascade() -> None:
    client = TestClient(app)

    response = client.post("/connectors/shopify/intake", json={"payload": _shopify_payload()})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["event"]["type"] == "sale"
    assert body["event"]["payload"]["sku"] == "4011"
    assert body["event"]["payload"]["location"] == "store_12"
    assert body["event"]["payload"]["unit_price"] == "30.00"
    assert body["pipeline"]["cascade"]["scenario"] == "pos_sale_price_integrity"
    assert body["pipeline"]["cascade"]["decision"]["action"]["type"] == "record_sale"


def test_shopify_multi_line_order_intake_persists_and_pipelines_every_line() -> None:
    """A multi-item order must not lose every line after the first (mapper) nor collapse
    them into one stored record (the inbound store's own dedup key)."""
    client = TestClient(app)
    payload = {
        "id": 9001,
        "created_at": "2026-07-06T10:00:00Z",
        "location_id": "store_12",
        "line_items": [
            {"id": 1, "sku": "4011", "quantity": 2, "price": "20.00"},
            {"id": 2, "sku": "4022", "quantity": 5, "price": "9.99"},
        ],
    }

    response = client.post("/connectors/shopify/intake", json={"payload": payload})
    records = client.get("/connectors/inbound-records")

    assert response.status_code == 200
    body = response.json()
    assert len(body["records"]) == 2
    assert [outcome["status"] for outcome in body["records"]] == ["accepted", "accepted"]
    assert [outcome["record"]["source_object_id"] for outcome in body["records"]] == [
        "9001:1",
        "9001:2",
    ]
    stored_ids = {rec["source_object_id"] for rec in records.json()["records"]}
    assert stored_ids == {"9001:1", "9001:2"}


def test_syspro_and_lightspeed_connector_intake_use_registered_mappers() -> None:
    client = TestClient(app)

    syspro = client.post(
        "/connectors/syspro/intake",
        json={
            "payload": {
                "StockCode": "4011",
                "Warehouse": "store_12",
                "QtyOnHand": "240",
            }
        },
    )
    lightspeed = client.post(
        "/connectors/lightspeed/intake",
        json={
            "payload": {
                "id": "ls_sale_1",
                "created_at": "2026-07-06T10:00:00Z",
                "shop_id": "store_12",
                "lines": [{"id": "line_1", "sku": "4011", "quantity": 2, "price": "30.00"}],
            }
        },
    )

    assert syspro.status_code == 200
    assert syspro.json()["status"] == "accepted"
    assert syspro.json()["event"]["type"] == "stock_update"
    assert lightspeed.status_code == 200
    assert lightspeed.json()["status"] == "accepted"
    assert lightspeed.json()["pipeline"]["cascade"]["scenario"] == "pos_sale_price_integrity"


def test_invalid_connector_record_is_persisted_without_event() -> None:
    client = TestClient(app)

    response = client.post(
        "/connectors/shopify/intake",
        json={"payload": {"id": 777, "created_at": "2026-07-06T10:00:00Z"}},
    )
    records = client.get("/connectors/inbound-records")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "invalid"
    assert body["event"] is None
    assert body["pipeline"] is None
    assert body["record"]["validation"]["ok"] is False
    assert "shopify order has no line_items" in body["record"]["validation"]["errors"]
    assert len(records.json()["records"]) == 1
    assert records.json()["records"][0]["validation"]["ok"] is False


def test_connector_intake_rejects_unknown_and_unmapped_systems() -> None:
    client = TestClient(app)

    unmapped = client.post("/connectors/dynamics/intake", json={"payload": {}})
    unknown = client.post("/connectors/not-a-system/intake", json={"payload": {}})

    assert unmapped.status_code == 422
    assert "no connector mapper registered" in unmapped.json()["detail"]
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Unknown connector system"


def test_connector_intake_uses_write_path_api_key_guard(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("API_KEY", "secret")

    blocked = client.post("/connectors/square/intake", json={"payload": _square_payload("241")})
    allowed = client.post(
        "/connectors/square/intake",
        json={"payload": _square_payload("241")},
        headers={"x-api-key": "secret"},
    )

    assert blocked.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "accepted"
