from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_storage import InMemoryTenantProfileStore, default_tenant_profile


def _token(role: str, *, tenant_id: str = "sa_retail_demo", secret: str = "secret") -> str:
    return encode_hs256_token(
        {
            "tenant_id": tenant_id,
            "user_id": "user_1",
            "role": role,
            "exp": int(time.time()) + 3600,
        },
        secret=secret,
    )


def test_tenant_profile_store_upserts_budgets_and_defaults() -> None:
    store = InMemoryTenantProfileStore()

    profile = store.upsert(
        {
            "tenant_id": "tenant_1",
            "name": "Kasi Grocer",
            "budgets": {"daily_request_limit": 25},
            "connector_policy": {"allowed_systems": ["csv", "odoo"]},
        }
    )
    updated = store.upsert(
        {
            "tenant_id": "tenant_1",
            "name": "Kasi Grocer Updated",
            "budgets": {"monthly_token_limit": 500_000},
        }
    )

    assert profile["tenant_id"] == "tenant_1"
    assert profile["budgets"]["daily_request_limit"] == 25
    assert profile["budgets"]["max_cascade_tokens"] == 24_000
    assert profile["connector_policy"]["write_back"] == "hitl_required"
    assert updated["name"] == "Kasi Grocer Updated"
    assert updated["created_at"] == profile["created_at"]
    assert updated["budgets"]["monthly_token_limit"] == 500_000
    assert store.list()[0]["tenant_id"] == "tenant_1"


def test_default_tenant_profile_is_za_retail_safe() -> None:
    profile = default_tenant_profile("tenant_2")

    assert profile["tenant_id"] == "tenant_2"
    assert profile["currency"] == "ZAR"
    assert profile["timezone"] == "Africa/Johannesburg"
    assert profile["connector_policy"]["mode"] == "read_only"
    assert profile["connector_policy"]["write_back"] == "hitl_required"


def test_tenant_profile_endpoint_onboards_current_tenant() -> None:
    client = TestClient(app)

    default_response = client.get("/tenants/me")
    upsert_response = client.post(
        "/tenants/me",
        json={
            "name": "Kasi Grocer",
            "budgets": {"daily_request_limit": 42},
            "model_limits": {"allow_external_models": False},
            "connector_policy": {
                "mode": "read_only",
                "allowed_systems": ["csv", "odoo"],
                "credential_ref": "vault://tenant/odoo",
            },
        },
    )
    get_response = client.get("/tenants/me")

    assert default_response.status_code == 200
    assert default_response.json()["profile"]["tenant_id"] == "sa_retail_demo"
    assert upsert_response.status_code == 200
    profile = upsert_response.json()["profile"]
    assert profile["tenant_id"] == "sa_retail_demo"
    assert profile["name"] == "Kasi Grocer"
    assert profile["budgets"]["daily_request_limit"] == 42
    assert profile["connector_policy"]["credential_ref"] == "vault://tenant/odoo"
    assert get_response.json()["profile"]["name"] == "Kasi Grocer"


def test_tenant_profile_rejects_inline_connector_secrets() -> None:
    client = TestClient(app)

    response = client.post(
        "/tenants/me",
        json={
            "name": "Kasi Grocer",
            "connector_policy": {"api_key": "plain-text-secret"},
        },
    )

    assert response.status_code == 422
    assert "secret references" in response.text


def test_tenant_profile_write_requires_owner_in_jwt_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    manager = {"Authorization": f"Bearer {_token('manager')}"}
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    blocked = client.post("/tenants/me", json={"name": "Blocked"}, headers=manager)
    allowed = client.post("/tenants/me", json={"name": "Allowed"}, headers=owner)
    current = client.get("/tenants/me", headers=owner)

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["profile"]["name"] == "Allowed"
    assert current.json()["profile"]["name"] == "Allowed"
