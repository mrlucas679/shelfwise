from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, write_limiter


def _delivery_payload() -> dict[str, object]:
    return {
        "sku": "milk_2l",
        "ordered_units": 50,
        "asn_units": 50,
        "received_units": 38,
        "accepted_units": 32,
        "short_dated_units": 6,
    }


def test_fefo_split_endpoint_exposes_batch_level_numbers() -> None:
    client = TestClient(app)

    response = client.post(
        "/intelligence/stock/fefo-split",
        json={
            "sku": "milk_2l",
            "as_of": "2026-07-06",
            "batches": [
                {
                    "sku": "milk_2l",
                    "lot": "MILK-OLD-0707",
                    "units": 10,
                    "expiry_date": "2026-07-07",
                    "received_date": "2026-07-03",
                    "location": "fridge_a",
                },
                {
                    "sku": "milk_2l",
                    "lot": "MILK-NEW-0713",
                    "units": 20,
                    "expiry_date": "2026-07-13",
                    "received_date": "2026-07-06",
                    "location": "fridge_a",
                },
            ],
        },
    )

    assert response.status_code == 200
    split = response.json()["batch_split"]
    assert split["total_units"] == 30
    assert split["priority_sell_units"] == 10
    assert split["normal_units"] == 20
    assert split["fefo_batches"][0]["lot"] == "MILK-OLD-0707"


def test_delivery_reconciliation_endpoint_flags_receiving_gap() -> None:
    client = TestClient(app)

    response = client.post(
        "/intelligence/deliveries/reconcile",
        json=_delivery_payload(),
    )

    assert response.status_code == 200
    reconciliation = response.json()["delivery_reconciliation"]
    assert reconciliation["status"] == "exception"
    assert reconciliation["missing_units"] == 12
    assert reconciliation["supplier_fill_rate"] == "0.76"


def test_supplier_cover_endpoint_recommends_transfer_before_stockout() -> None:
    client = TestClient(app)

    response = client.post(
        "/intelligence/suppliers/cover-plan",
        json={
            "sku": "milk_2l",
            "units_on_hand": 12,
            "forecast_daily_units": "10",
            "supplier_lead_time_days": "3",
            "transfer_available_units": 18,
        },
    )

    assert response.status_code == 200
    cover = response.json()["supplier_cover"]
    assert cover["recommended_action"] == "transfer"
    assert cover["gap_before_delivery_units"] == 18
    assert cover["transfer_units_recommended"] == 18


def test_outcome_summary_endpoint_returns_learning_signal() -> None:
    client = TestClient(app)

    response = client.post(
        "/intelligence/outcomes/summarize",
        json={
            "sku": "yoghurt_1l",
            "action": "markdown",
            "predicted_sell_through_units": 24,
            "actual_sell_through_units": 30,
            "predicted_waste_units": 8,
            "actual_waste_units": 5,
        },
    )

    assert response.status_code == 200
    learning = response.json()["learning_summary"]
    assert learning["sell_through_delta_units"] == 6
    assert learning["waste_delta_units"] == -3
    assert learning["score"] == "0.72"


def test_fefo_split_endpoint_rejects_cross_sku_batches() -> None:
    client = TestClient(app)

    response = client.post(
        "/intelligence/stock/fefo-split",
        json={
            "sku": "milk_2l",
            "as_of": "2026-07-06",
            "batches": [
                {
                    "sku": "bread_700g",
                    "lot": "BREAD-0707",
                    "units": 6,
                    "expiry_date": "2026-07-07",
                    "received_date": "2026-07-06",
                    "location": "bakery",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "all batches must match sku"


def test_intelligence_routes_require_the_shared_write_key_when_configured(monkeypatch) -> None:
    """Calculation endpoints are still write-path resource consumers in JWT deployments."""
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-tenant-secret")
    monkeypatch.setenv("API_KEY", "intelligence-test-key")
    client = TestClient(app)
    requests = (
        ("/intelligence/deliveries/reconcile", _delivery_payload()),
        (
            "/intelligence/suppliers/cover-plan",
            {
                "sku": "milk_2l",
                "units_on_hand": 12,
                "forecast_daily_units": "10",
                "supplier_lead_time_days": "3",
            },
        ),
        (
            "/intelligence/outcomes/summarize",
            {
                "sku": "milk_2l",
                "action": "markdown",
                "predicted_sell_through_units": 10,
                "actual_sell_through_units": 12,
                "predicted_waste_units": 2,
                "actual_waste_units": 1,
            },
        ),
    )

    for path, payload in requests:
        assert client.post(path, json=payload).status_code == 401

    allowed = client.post(
        "/intelligence/deliveries/reconcile",
        json=_delivery_payload(),
        headers={"x-api-key": "intelligence-test-key"},
    )
    assert allowed.status_code == 200


def test_intelligence_routes_share_the_write_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "intelligence-test-key")
    write_limiter.configure(capacity=1, refill_per_s=0.0, max_keys=1024)
    client = TestClient(app)
    headers = {"x-api-key": "intelligence-test-key"}
    try:
        assert client.post(
            "/intelligence/deliveries/reconcile", json=_delivery_payload(), headers=headers
        ).status_code == 200
        assert client.post(
            "/intelligence/deliveries/reconcile", json=_delivery_payload(), headers=headers
        ).status_code == 429
    finally:
        write_limiter.configure(capacity=240, refill_per_s=8.0, max_keys=1024)
