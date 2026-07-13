from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


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
