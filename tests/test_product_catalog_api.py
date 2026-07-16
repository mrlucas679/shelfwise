from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.product_catalog import _product_item


def test_product_attention_endpoint_returns_bounded_product_groups() -> None:
    client = TestClient(app)

    response = client.get("/products/attention?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert len(body["items"]) <= 2
    assert body["totals"]["attention_products"] >= 1
    assert body["totals"]["sell_first_products"] >= 1
    assert any(item["requires_attention"] for item in body["items"])
    assert all(item["lot_count"] == len(item["batches"]) for item in body["items"])
    assert all(item["batches"] for item in body["items"])
    assert body["totals"]["candidates"] >= 1
    assert body["candidates"]


def test_product_search_endpoint_is_search_first_and_attention_ranked() -> None:
    client = TestClient(app)

    summary = client.get("/data/seed/summary").json()["seed_data"]
    response = client.get(
        "/products/search", params={"q": summary["product_name"], "limit": 5}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == summary["product_name"]
    assert len(body["products"]) <= 5
    first = body["products"][0]
    assert first["sku"] == summary["sku"]
    assert first["name"] == summary["product_name"]
    assert first["source"] == "generated_world"
    assert body["source_counts"]["generated_world"] >= 1


def test_empty_product_search_does_not_return_full_catalogue() -> None:
    client = TestClient(app)

    response = client.get("/products/search?limit=3")

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == ""
    assert len(body["products"]) <= 3
    assert all(item["requires_attention"] for item in body["products"])


def test_product_search_rejects_unbounded_limits() -> None:
    client = TestClient(app)

    response = client.get("/products/search?q=milk&limit=500")

    assert response.status_code == 422
    assert response.json()["detail"] == "limit must be between 1 and 50"


def test_product_search_is_bounded_by_the_generated_world_limit() -> None:
    client = TestClient(app)
    response = client.get("/products/search", params={"q": "missing-product", "limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["products"] == []
    assert body["source_counts"]["generated_world"] == 0


def test_operational_product_search_reads_catalog_and_twin_without_world_fallback() -> None:
    client = TestClient(app)
    observed_at = datetime.now(UTC).isoformat()
    client.post(
        "/catalog/products",
        json={"product_id": "prod_ops", "name": "Operational Milk", "category": "dairy"},
    )
    client.post(
        "/catalog/products/prod_ops/variants",
        json={"variant_id": "var_ops", "pack_size": "1L"},
    )
    client.post(
        "/catalog/identifiers",
        json={"variant_id": "var_ops", "kind": "sku", "value": "SKU-OPS"},
    )
    client.post(
        "/ingest",
        json={
            "id": "evt_ops_stock",
            "type": "stock_update",
            "ts": observed_at,
            "actor": "store_ops",
            "tenant_id": "sa_retail_demo",
            "data_domain": "operational_twin",
            "payload": {
                "store_id": "store_ops",
                "sku": "SKU-OPS",
                "product": "Operational Milk",
                "category": "dairy",
                "supplier": "SUP-OPS",
                "on_hand": 18,
                "reorder_point": 20,
                "unit_cost": "12.00",
                "unit_price": "20.00",
            },
        },
    )
    client.post(
        "/ingest",
        json={
            "id": "evt_ops_expiry",
            "type": "expiry_entry",
            "ts": observed_at,
            "actor": "store_ops",
            "tenant_id": "sa_retail_demo",
            "data_domain": "operational_twin",
            "payload": {
                "store_id": "store_ops",
                "sku": "SKU-OPS",
                "days_to_expiry": 2,
                "batch_id": "LOT-OPS",
            },
        },
    )

    response = client.get(
        "/products/search",
        params={
            "q": "Operational Milk",
            "data_domain": "operational_twin",
            "store_id": "store_ops",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data_domain"] == "operational_twin"
    assert body["source_counts"] == {"operational_twin": 1}
    assert body["products"][0]["source"] == "operational_twin"
    assert body["products"][0]["sku"] == "SKU-OPS"


def test_operational_seed_summary_reports_missing_measurements_without_demo_fallback() -> None:
    response = TestClient(app).get(
        "/data/seed/summary?data_domain=operational_twin"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data_domain"] == "operational_twin"
    assert body["seed_data"] is None
    assert body["status"] == "insufficient_operational_facts"
    assert "sku" in body["missing_data"]


def test_open_shipment_suppresses_covered_low_stock_candidate() -> None:
    client = TestClient(app)
    initial = client.get("/products/attention?limit=50").json()
    low_stock = next(
        item
        for item in initial["candidates"]
        if item["candidate_type"] == "low_stock"
    )
    evidence = low_stock["evidence"]
    gap = int(evidence["reorder_point"]) - int(low_stock["exposure_units"])
    assert gap > 0

    response = client.post(
        "/ingest",
        json={
            "id": "evt_open_order_candidate_test",
            "type": "shipment",
            "ts": "2026-07-13T10:00:00Z",
            "actor": "wms",
            "source": "api",
            "tenant_id": "sa_retail_demo",
            "data_domain": "world_simulation",
            "payload": {
                "order_id": "PO-CANDIDATE-TEST",
                "sku": low_stock["sku"],
                "supplier_id": "supplier-test",
                "ordered_units": gap,
                "eta": "2026-07-15T10:00:00Z",
            },
        },
    )

    refreshed = client.get("/products/attention?limit=50").json()
    matching = next(
        item
        for item in refreshed["candidates"]
        if item["candidate_key"] == low_stock["candidate_key"]
    )
    assert response.status_code == 200
    assert matching["status"] == "suppressed"
    assert "open order covers" in matching["suppression_reason"]


def test_expired_lot_is_blocked_instead_of_counted_as_sell_first() -> None:
    item = _product_item(
        {
            "sku": "SKU-1",
            "name": "Milk",
            "category": "Dairy",
            "supplier": "Supplier",
            "unit_price": 20,
            "unit_cost": 10,
        },
        {
            "sku": "SKU-1",
            "on_hand": 12,
            "reorder_point": 4,
            "expiry_date": "2026-07-12",
            "batches": [
                {
                    "lot_id": "LOT-OLD",
                    "on_hand": 7,
                    "expiry_date": "2026-07-12",
                    "received_date": "2026-07-01",
                },
                {
                    "lot_id": "LOT-FRESH",
                    "on_hand": 5,
                    "expiry_date": "2026-07-15",
                    "received_date": "2026-07-10",
                },
            ],
        },
        as_of=date(2026, 7, 13),
    )

    assert item["blocked_units"] == 7
    assert item["sell_first_units"] == 5
    assert item["normal_units"] == 0
    assert "blocked" in item["attention_reasons"]


def test_product_item_exposes_operational_signals_and_policy_expiry_window() -> None:
    item = _product_item(
        {
            "sku": "FROZEN-1",
            "name": "Frozen peas",
            "category": "Frozen",
            "physics": "frozen",
            "supplier": "Cold Supplier",
            "unit_price": 30,
            "unit_cost": 15,
        },
        {
            "sku": "FROZEN-1",
            "on_hand": 100,
            "reorder_point": 10,
            "expiry_date": "2026-07-17",
            "batches": [
                {
                    "lot_id": "LOT-FROZEN-1",
                    "on_hand": 100,
                    "expiry_date": "2026-07-17",
                }
            ],
        },
        as_of=date(2026, 7, 13),
        supplier={"recent_delay": True, "lead_time_days": 4},
        recent_daily_units=[1, 1, 1],
    )

    assert "expiring" in item["attention_reasons"]
    assert "supplier_delay" in item["attention_reasons"]
    assert "slow_mover" in item["attention_reasons"]
    assert item["policy"]["expiry_review_days"] == 5
