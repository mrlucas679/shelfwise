from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import shelfwise_connectors.connector_test as connector_test_module
from shelfwise_backend.app import app
from shelfwise_backend.state import connector_credential_store
from shelfwise_backend.tenant import encode_hs256_token


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


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield
    connector_credential_store.clear()


def _jwt_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    monkeypatch.setenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "route-test-key")


def test_credential_write_requires_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    manager = {"Authorization": f"Bearer {_token('manager')}"}
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    blocked = client.post(
        "/connectors/odoo/credentials",
        json={"fields": {"api_key": "secret"}},
        headers=manager,
    )
    allowed = client.post(
        "/connectors/odoo/credentials",
        json={"fields": {"api_key": "secret"}},
        headers=owner,
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {"system": "odoo", "configured": True}


def test_credential_status_never_returns_plaintext_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    client.post(
        "/connectors/odoo/credentials",
        json={"fields": {"api_key": "super-secret-value"}},
        headers=owner,
    )
    status = client.get("/connectors/odoo/credentials", headers=owner)

    assert status.status_code == 200
    assert status.json() == {"system": "odoo", "configured": True}
    assert "super-secret-value" not in status.text


def test_credential_status_is_false_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    status = client.get("/connectors/sap/credentials", headers=owner)

    assert status.json() == {"system": "sap", "configured": False}


def test_credential_delete_removes_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    client.post(
        "/connectors/sap/credentials", json={"fields": {"token": "x"}}, headers=owner
    )
    delete_response = client.post("/connectors/sap/credentials/delete", headers=owner)
    status = client.get("/connectors/sap/credentials", headers=owner)

    assert delete_response.json() == {"system": "sap", "configured": False}
    assert status.json()["configured"] is False


def test_unknown_connector_system_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    response = client.get("/connectors/not_a_real_system/credentials", headers=owner)

    assert response.status_code == 404


def test_connection_test_reports_ok_with_unsaved_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)

    async def fake_fetch_page(self, cursor):  # noqa: ARG001 - matches PollingConnector shape
        return [], None

    monkeypatch.setattr(
        connector_test_module.SapS4InventoryConnector, "fetch_page", fake_fetch_page
    )
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    response = client.post(
        "/connectors/sap/credentials/test",
        json={"fields": {"base_url": "https://sap.example.com", "token": "tok"}},
        headers=owner,
    )

    assert response.status_code == 200
    assert response.json() == {
        "system": "sap",
        "status": "ok",
        "ok": True,
        "detail": "connected successfully",
    }


def test_connection_test_reports_auth_error_without_saving_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)

    async def failing_fetch_page(self, cursor):  # noqa: ARG001
        request = httpx.Request("GET", "https://sap.example.com")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(
        connector_test_module.SapS4InventoryConnector, "fetch_page", failing_fetch_page
    )
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    response = client.post(
        "/connectors/sap/credentials/test",
        json={"fields": {"base_url": "https://sap.example.com", "token": "bad"}},
        headers=owner,
    )
    status = client.get("/connectors/sap/credentials", headers=owner)

    assert response.status_code == 200
    assert response.json()["status"] == "auth_error"
    assert status.json()["configured"] is False


def test_connection_test_uses_stored_credentials_when_no_fields_posted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)

    async def fake_fetch_page(self, cursor):  # noqa: ARG001
        return [], None

    monkeypatch.setattr(
        connector_test_module.SapS4InventoryConnector, "fetch_page", fake_fetch_page
    )
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}
    client.post(
        "/connectors/sap/credentials",
        json={"fields": {"base_url": "https://sap.example.com", "token": "tok"}},
        headers=owner,
    )

    response = client.post("/connectors/sap/credentials/test", json={}, headers=owner)

    assert response.json()["status"] == "ok"


def test_connection_test_on_webhook_only_system_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    response = client.post("/connectors/shopify/credentials/test", json={}, headers=owner)

    assert response.status_code == 422


def test_connection_test_requires_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    manager = {"Authorization": f"Bearer {_token('manager')}"}

    response = client.post(
        "/connectors/sap/credentials/test",
        json={"fields": {"base_url": "https://sap.example.com", "token": "tok"}},
        headers=manager,
    )

    assert response.status_code == 403


def test_tenants_cannot_read_each_others_credential_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    tenant_a_owner = {
        "Authorization": f"Bearer {_token('owner', tenant_id='tenant_a')}"
    }
    tenant_b_owner = {
        "Authorization": f"Bearer {_token('owner', tenant_id='tenant_b')}"
    }

    client.post(
        "/connectors/odoo/credentials",
        json={"fields": {"api_key": "tenant-a-secret"}},
        headers=tenant_a_owner,
    )
    tenant_b_status = client.get("/connectors/odoo/credentials", headers=tenant_b_owner)

    assert tenant_b_status.json() == {"system": "odoo", "configured": False}
