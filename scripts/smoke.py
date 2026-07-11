from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shelfwise_backend.app import app  # noqa: E402


def main() -> None:
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200, health.text

    run = client.post("/demo/golden")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["decision"]["status"] == "pending"
    assert body["decision"]["action"]["type"] == "apply_markdown"

    trace = client.get(f"/trace/{body['correlation_id']}")
    assert trace.status_code == 200, trace.text
    assert trace.json()["trace"]["decision_id"] == body["decision"]["id"]

    approve = client.post(f"/decisions/{body['decision']['id']}/approve")
    assert approve.status_code == 200, approve.text
    assert approve.json()["decision"]["status"] == "approved"

    rejection = client.get("/demo/critic-rejection")
    assert rejection.status_code == 200, rejection.text
    rejected = rejection.json()["decision"]
    assert rejected["status"] == "rejected"
    assert rejected["action"]["type"] == "monitor"
    assert rejected["critic_verdict"] == "rejected"

    attention = client.get("/products/attention?limit=2")
    assert attention.status_code == 200, attention.text
    attention_body = attention.json()
    assert len(attention_body["items"]) <= 2
    assert attention_body["totals"]["sell_first_products"] >= 1

    product_search = client.get("/products/search?q=amasi&limit=3")
    assert product_search.status_code == 200, product_search.text
    product_body = product_search.json()
    assert product_body["products"][0]["name"] == "Amasi 2L"
    assert product_body["products"][0]["sell_first_units"] == 10
    assert product_body["products"][0]["fefo_batches"][0]["lot"] == "AMASI-OLD-0707"

    fefo = client.post(
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
    assert fefo.status_code == 200, fefo.text
    assert fefo.json()["batch_split"]["priority_sell_units"] == 10

    delivery = client.post(
        "/intelligence/deliveries/reconcile",
        json={
            "sku": "milk_2l",
            "ordered_units": 50,
            "asn_units": 50,
            "received_units": 38,
            "accepted_units": 32,
            "short_dated_units": 6,
        },
    )
    assert delivery.status_code == 200, delivery.text
    assert delivery.json()["delivery_reconciliation"]["missing_units"] == 12

    supplier = client.post(
        "/intelligence/suppliers/cover-plan",
        json={
            "sku": "milk_2l",
            "units_on_hand": 12,
            "forecast_daily_units": "10",
            "supplier_lead_time_days": "3",
            "transfer_available_units": 18,
        },
    )
    assert supplier.status_code == 200, supplier.text
    assert supplier.json()["supplier_cover"]["recommended_action"] == "transfer"

    outcome = client.post(
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
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["learning_summary"]["score"] == "0.72"

    print("SMOKE OK")


if __name__ == "__main__":
    main()
