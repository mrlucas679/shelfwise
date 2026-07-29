from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, decision_store
from shelfwise_backend.decision_assignment import filter_assigned_decisions
from shelfwise_backend.tenant import Role, encode_hs256_token


def test_assignment_matrix_filters_inventory_without_hiding_manager_work() -> None:
    decisions = [
        {"id": "store", "role": "store_manager"},
        {"id": "inventory", "role": "inventory_manager"},
        {"id": "procurement", "role": "procurement_manager"},
        {"id": "facilities", "role": "facilities_manager"},
    ]

    inventory_ids = {
        decision["id"]
        for decision in filter_assigned_decisions(decisions, role=Role.INVENTORY)
    }
    manager_ids = {
        decision["id"]
        for decision in filter_assigned_decisions(decisions, role=Role.MANAGER)
    }

    assert inventory_ids == {"inventory", "procurement"}
    assert manager_ids == {"store", "inventory", "procurement", "facilities"}


def test_assigned_decision_queue_derives_role_from_verified_session(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "decision-assignment-secret")
    for decision in (
        {
            "id": "dec_inventory",
            "tenant_id": "queue_tenant",
            "data_domain": "operational_twin",
            "role": "inventory_manager",
            "status": "pending",
        },
        {
            "id": "dec_store",
            "tenant_id": "queue_tenant",
            "data_domain": "operational_twin",
            "role": "store_manager",
            "status": "pending",
        },
    ):
        decision_store.upsert(decision)

    token = encode_hs256_token(
        {
            "tenant_id": "queue_tenant",
            "user_id": "inventory_1",
            "role": "inventory",
        },
        secret="decision-assignment-secret",
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    assigned = client.get(
        "/decisions?data_domain=operational_twin&queue_view=assigned",
        headers=headers,
    )
    full_ledger = client.get(
        "/decisions?data_domain=operational_twin",
        headers=headers,
    )

    assert assigned.status_code == 200
    assert assigned.json()["queue_view"] == "assigned"
    assert assigned.json()["assignment"]["account_role"] == "inventory"
    assert [item["id"] for item in assigned.json()["decisions"]] == ["dec_inventory"]
    assert {item["id"] for item in full_ledger.json()["decisions"]} == {
        "dec_inventory",
        "dec_store",
    }
