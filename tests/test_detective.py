from __future__ import annotations

from _world_test_support import demo_sku
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.detective import analyze_root_cause, root_cause_cte_sql


def test_detective_traces_decision_to_event_root() -> None:
    analysis = analyze_root_cause(
        "dec_1",
        events=[
            {
                "id": "evt_1",
                "type": "scan",
                "tenant_id": "tenant_1",
                "correlation_id": "cor_1",
                "causation_id": None,
            }
        ],
        decisions=[
            {
                "id": "dec_1",
                "tenant_id": "tenant_1",
                "status": "pending",
                "caused_by": ["cor_1"],
                "action": {"type": "apply_markdown"},
            }
        ],
    )

    assert analysis.found is True
    assert analysis.root_events[0].id == "evt_1"
    assert [node.id for node in analysis.paths[0]] == ["dec_1", "evt_1"]


def test_root_cause_cte_uses_recursive_tenant_scoped_sql() -> None:
    sql = root_cause_cte_sql().lower()

    assert "with recursive lineage" in sql
    assert "current_setting('app.tenant_id', true)" in sql
    assert "shelfwise_decisions" in sql
    assert "shelfwise_events" in sql


def test_detective_endpoint_reports_ingested_scan_root_cause() -> None:
    client = TestClient(app)
    ingest = client.post(
        "/ingest",
        json={
            "id": "evt_detective_scan",
            "type": "scan",
            "ts": "2026-07-06T10:14:00Z",
            "actor": "store_12",
            "source": "scanner",
            "tenant_id": "sa_retail_demo",
            "payload": {"sku": demo_sku(), "location": "store_12"},
        },
    )
    decision_id = ingest.json()["cascade"]["decision"]["id"]

    response = client.get(f"/detective/root-cause/{decision_id}")
    sql_response = client.get("/detective/root-cause-sql")

    assert response.status_code == 200
    analysis = response.json()["analysis"]
    assert analysis["target_id"] == decision_id
    assert analysis["root_events"][0]["id"] == "evt_detective_scan"
    assert analysis["paths"][0][0]["kind"] == "decision"
    assert sql_response.status_code == 200
    assert "with recursive" in sql_response.json()["sql"].lower()
