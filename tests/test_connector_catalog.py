from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_connectors import (
    SourceSystem,
    connector_status_for_policy,
    list_connector_capabilities,
)


def test_connector_catalog_lists_available_and_roadmap_systems() -> None:
    systems = {item.system for item in list_connector_capabilities()}

    assert {
        SourceSystem.CSV,
        SourceSystem.ODOO,
        SourceSystem.SQUARE,
        SourceSystem.SAP,
        SourceSystem.SHOPIFY,
        SourceSystem.SYSPRO,
        SourceSystem.LIGHTSPEED,
    } <= systems


def test_connector_status_reflects_tenant_policy_and_mapper_coverage() -> None:
    rows = connector_status_for_policy({"allowed_systems": ["CSV", "odoo", "syspro"]})
    by_system = {row["system"]: row for row in rows}

    assert by_system["csv"]["enabled_for_tenant"] is True
    assert by_system["csv"]["status"] == "enabled"
    assert by_system["odoo"]["status"] == "enabled"
    assert by_system["syspro"]["enabled_for_tenant"] is True
    assert by_system["syspro"]["status"] == "enabled"
    assert by_system["square"]["enabled_for_tenant"] is False
    assert by_system["square"]["status"] == "available"
    assert by_system["lightspeed"]["status"] == "available"


def test_connector_status_accepts_single_allowed_system_string() -> None:
    by_system = {
        row["system"]: row
        for row in connector_status_for_policy({"allowed_systems": "square"})
    }

    assert by_system["square"]["enabled_for_tenant"] is True
    assert by_system["square"]["status"] == "enabled"
    assert by_system["csv"]["enabled_for_tenant"] is False


def test_connector_catalog_endpoints_reflect_tenant_profile_policy() -> None:
    client = TestClient(app)

    systems_response = client.get("/connectors/systems")
    profile_response = client.post(
        "/tenants/me",
        json={
            "name": "Kasi Grocer",
            "connector_policy": {"allowed_systems": ["csv", "odoo", "syspro"]},
        },
    )
    mine_response = client.get("/connectors/me")

    assert systems_response.status_code == 200
    assert any(row["system"] == "square" for row in systems_response.json()["systems"])
    assert profile_response.status_code == 200
    assert mine_response.status_code == 200
    body = mine_response.json()
    by_system = {row["system"]: row for row in body["systems"]}
    assert body["tenant_id"] == "sa_retail_demo"
    assert by_system["csv"]["enabled_for_tenant"] is True
    assert by_system["odoo"]["status"] == "enabled"
    assert by_system["syspro"]["status"] == "enabled"
