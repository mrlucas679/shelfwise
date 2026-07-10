from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend import product_catalog
from shelfwise_backend.app import app
from shelfwise_worldgen.catalog.model import CatalogProduct


def test_product_attention_endpoint_returns_bounded_product_groups() -> None:
    client = TestClient(app)

    response = client.get("/products/attention?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert len(body["items"]) <= 2
    assert body["totals"]["attention_products"] >= 1
    assert body["totals"]["sell_first_products"] >= 1
    assert any(item["sku"] == "4011" and item["name"] == "Amasi 2L" for item in body["items"])


def test_product_search_endpoint_is_search_first_and_attention_ranked() -> None:
    client = TestClient(app)

    response = client.get("/products/search?q=amasi&limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "amasi"
    assert len(body["products"]) <= 5
    first = body["products"][0]
    assert first["sku"] == "4011"
    assert first["name"] == "Amasi 2L"
    assert "sell_first" in first["attention_reasons"]
    assert first["sell_first_units"] == 10
    assert first["lot_count"] == 2
    assert first["fefo_batches"][0]["lot"] == "AMASI-OLD-0707"
    assert first["fefo_batches"][0]["stock_status"] == "priority_sell"
    assert (
        body["source_counts"]["synthetic_scanned"]
        <= body["source_counts"]["synthetic_scan_budget"]
    )
    assert (
        body["source_counts"]["synthetic_total_estimate"]
        >= body["source_counts"]["synthetic_scanned"]
    )


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


def test_product_search_stops_at_synthetic_scan_budget(monkeypatch) -> None:
    def many_products(_seed: int, *, scale: str):
        del scale
        for index in range(1_000):
            yield _synthetic_product(index)

    monkeypatch.setattr(product_catalog, "count_estimate", lambda _scale: 500_000)
    monkeypatch.setattr(product_catalog, "generate_catalog", many_products)

    result = product_catalog.search_product_catalog(
        query="missing-product",
        limit=5,
        synthetic_scan_budget=7,
    )

    assert result["products"] == []
    assert result["truncated"] is True
    assert result["source_counts"]["synthetic_scanned"] == 7
    assert result["source_counts"]["synthetic_scan_budget"] == 7
    assert result["source_counts"]["synthetic_total_estimate"] == 500_000


def _synthetic_product(index: int) -> CatalogProduct:
    return CatalogProduct(
        product_id=f"P{index:08d}",
        barcode=f"2000000000{index:02d}",
        plu=None,
        name=f"Generated Item {index}",
        receipt_name=f"GENERATED {index}",
        brand="Generated",
        generic_name="Item",
        department="Grocery",
        category="Pantry",
        subcategory="Staples",
        physics="ambient_long",
        size_label="1 kg",
        unit="each",
        price_cents=999,
        vat_rate=0.15,
        supplier="Generated SA",
        shelf_location="A-01",
    )
