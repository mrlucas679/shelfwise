from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, decision_store, writeback_sink
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_backend.value_reporting import monthly_value_statement


def test_monthly_statement_never_counts_model_outcomes_as_verified_value() -> None:
    report = monthly_value_statement(
        [
            {
                "id": "verified",
                "status": "approved",
                "updated_at": "2026-07-10T12:00:00+00:00",
                "outcome": {"rand_recovered": {"minor_units": 999_999}},
                "verified_outcome": {
                    "minor_units": 12_500,
                    "verified_at": "2026-07-11T12:00:00+00:00",
                    "source_reference": "pos:receipt-1",
                    "task_id": "task_1",
                    "verified_by": "manager_1",
                },
            },
            {
                "id": "estimated",
                "status": "approved",
                "updated_at": "2026-07-12T12:00:00+00:00",
                "expected_outcome": {"incremental_profit_minor_units": 4_000},
                "outcome": {"rand_recovered": {"minor_units": 88_000}},
            },
        ],
        month="2026-07",
    )

    assert report["verified_recovered_minor_units"] == 12_500
    assert report["verified_receipt_count"] == 1
    assert report["estimated_opportunity_minor_units"] == 4_000
    assert report["unverified_decision_count"] == 1


def test_task_completion_records_value_receipt_on_operational_decision() -> None:
    decision_store.upsert(
        {
            "id": "dec_value_receipt",
            "tenant_id": "sa_retail_demo",
            "data_domain": "operational_twin",
            "status": "approved",
            "role": "store_manager",
        }
    )
    task = writeback_sink.create_task(
        idempotency_key="writeback:dec_value_receipt",
        tenant_id="sa_retail_demo",
        data_domain="operational_twin",
        title="Verify completed action",
        action={"type": "manual_check", "params": {"units": 2}},
        rollback_instructions={"decision_id": "dec_value_receipt"},
    )
    client = TestClient(app)

    completed = client.post(
        f"/writeback/tasks/{task['id']}/complete",
        json={
            "source_reference": "pos:receipt-2026-07-001",
            "completed_units": 2,
            "actual_value_recovered_minor_units": 7_500,
            "currency": "ZAR",
        },
    )
    report = client.get("/reports/value-recovered?month=2026-07")

    assert completed.status_code == 200
    assert report.status_code == 200
    assert report.json()["data_domain"] == "operational_twin"
    statement = report.json()["report"]
    assert statement["verified_recovered_minor_units"] == 7_500
    assert statement["verified_receipts"][0]["decision_id"] == "dec_value_receipt"
    assert statement["verified_receipts"][0]["source_reference"] == (
        "pos:receipt-2026-07-001"
    )


def test_value_report_rejects_invalid_month_and_excludes_simulation() -> None:
    decision_store.upsert(
        {
            "id": "dec_simulated_value",
            "tenant_id": "sa_retail_demo",
            "data_domain": "world_simulation",
            "status": "approved",
            "verified_outcome": {
                "minor_units": 1_000_000,
                "verified_at": "2026-07-11T12:00:00+00:00",
            },
        }
    )
    client = TestClient(app)

    assert client.get("/reports/value-recovered?month=July").status_code == 422
    assert (
        client.get("/reports/value-recovered?month=2026-07")
        .json()["report"]["verified_recovered_minor_units"]
        == 0
    )


def test_value_report_is_tenant_scoped_in_jwt_mode(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "value-report-secret")
    for tenant_id, amount in (("value_tenant_a", 1_500), ("value_tenant_b", 9_000)):
        decision_store.upsert(
            {
                "id": f"dec_{tenant_id}",
                "tenant_id": tenant_id,
                "data_domain": "operational_twin",
                "status": "approved",
                "verified_outcome": {
                    "minor_units": amount,
                    "verified_at": "2026-07-11T12:00:00+00:00",
                    "source_reference": f"receipt:{tenant_id}",
                    "task_id": f"task_{tenant_id}",
                    "verified_by": "manager_1",
                },
            }
        )
    token = encode_hs256_token(
        {
            "tenant_id": "value_tenant_a",
            "user_id": "owner_a",
            "role": "owner",
        },
        secret="value-report-secret",
    )

    response = TestClient(app).get(
        "/reports/value-recovered?month=2026-07",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "value_tenant_a"
    assert response.json()["report"]["verified_recovered_minor_units"] == 1_500
