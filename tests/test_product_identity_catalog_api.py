from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def test_product_variant_and_identifier_lifecycle_through_the_api() -> None:
    client = TestClient(app)

    product = client.post(
        "/catalog/products",
        json={"product_id": "prod_milk", "name": "Full Cream Milk", "category": "dairy"},
    )
    variant = client.post(
        "/catalog/products/prod_milk/variants",
        json={"variant_id": "var_milk_1l", "pack_size": "1L", "unit_of_measure": "each"},
    )
    identifier = client.post(
        "/catalog/identifiers",
        json={"variant_id": "var_milk_1l", "kind": "barcode", "value": "6001234567890"},
    )
    resolved = client.get("/catalog/resolve?kind=barcode&value=6001234567890")
    listed_products = client.get("/catalog/products")
    listed_variants = client.get("/catalog/products/prod_milk/variants")
    fetched_product = client.get("/catalog/products/prod_milk")
    missing = client.get("/catalog/resolve?kind=barcode&value=does-not-exist")

    assert product.status_code == 200
    assert product.json()["product"]["name"] == "Full Cream Milk"
    assert variant.status_code == 200
    assert variant.json()["variant"]["product_id"] == "prod_milk"
    assert identifier.status_code == 200
    assert resolved.status_code == 200
    assert resolved.json()["variant"]["variant_id"] == "var_milk_1l"
    assert listed_products.status_code == 200
    assert len(listed_products.json()["products"]) == 1
    assert listed_variants.status_code == 200
    assert len(listed_variants.json()["variants"]) == 1
    assert fetched_product.status_code == 200
    assert fetched_product.json()["product"]["product_id"] == "prod_milk"
    assert missing.status_code == 404


def test_conflicting_identifier_remap_returns_409_not_a_silent_overwrite() -> None:
    client = TestClient(app)
    client.post("/catalog/products", json={"product_id": "prod_a", "name": "A"})
    client.post("/catalog/products/prod_a/variants", json={"variant_id": "var_a"})
    client.post("/catalog/products", json={"product_id": "prod_b", "name": "B"})
    client.post("/catalog/products/prod_b/variants", json={"variant_id": "var_b"})
    client.post(
        "/catalog/identifiers", json={"variant_id": "var_a", "kind": "gtin", "value": "shared"}
    )

    conflict = client.post(
        "/catalog/identifiers", json={"variant_id": "var_b", "kind": "gtin", "value": "shared"}
    )

    assert conflict.status_code == 409


def test_unknown_product_returns_404() -> None:
    client = TestClient(app)

    response = client.get("/catalog/products/does-not-exist")

    assert response.status_code == 404


def test_invalid_identifier_kind_returns_422() -> None:
    client = TestClient(app)
    client.post("/catalog/products", json={"product_id": "prod_a", "name": "A"})
    client.post("/catalog/products/prod_a/variants", json={"variant_id": "var_a"})

    response = client.post(
        "/catalog/identifiers",
        json={"variant_id": "var_a", "kind": "not_a_real_kind", "value": "x"},
    )

    assert response.status_code == 422
