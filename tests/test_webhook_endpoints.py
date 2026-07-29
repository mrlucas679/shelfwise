"""Self-serve tenant webhook endpoints: provisioning, authentication, and isolation."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.state import webhook_endpoint_registry
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


def _jwt_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    monkeypatch.setenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "webhook-endpoint-test-key")


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _square_payload(catalog_object_id: str = "sku_1", quantity: str = "7") -> dict:
    return {
        "event_id": f"evt_{catalog_object_id}_{quantity}",
        "data": {
            "object": {
                "inventory_counts": [
                    {
                        "catalog_object_id": catalog_object_id,
                        "location_id": "store_12",
                        "quantity": quantity,
                    }
                ]
            }
        },
    }


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield
    webhook_endpoint_registry.clear()


def _provision(client: TestClient, headers: dict[str, str], system: str = "square") -> dict:
    response = client.post(f"/connectors/{system}/webhook-endpoint", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_owner_provisions_an_endpoint_and_receives_the_secret_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    provisioned = _provision(client, owner)
    listed = client.get("/connectors/webhook-endpoints", headers=owner)

    assert provisioned["signing_secret"]
    assert provisioned["endpoint_id"].startswith("whep_")
    assert provisioned["endpoint_id"] in provisioned["delivery_url"]
    assert listed.status_code == 200
    body = listed.json()
    assert [row["endpoint_id"] for row in body["endpoints"]] == [provisioned["endpoint_id"]]
    # The secret must never be recoverable after provisioning.
    assert provisioned["signing_secret"] not in listed.text


def test_a_correctly_signed_delivery_ingests_without_any_operator_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a store owner connects their POS with no developer involvement."""
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}
    provisioned = _provision(client, owner)
    raw = json.dumps(_square_payload()).encode("utf-8")

    response = client.post(
        f"/connectors/webhook/{provisioned['endpoint_id']}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-shelfwise-signature": _sign(provisioned["signing_secret"], raw),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["system"] == "square"
    assert response.json()["records"]


def test_a_wrongly_signed_delivery_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}
    provisioned = _provision(client, owner)
    raw = json.dumps(_square_payload()).encode("utf-8")

    response = client.post(
        f"/connectors/webhook/{provisioned['endpoint_id']}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-shelfwise-signature": _sign("not-the-real-secret", raw),
        },
    )

    assert response.status_code == 401


def test_a_tampered_body_no_longer_matches_its_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signing the body, not just holding the secret, is what authenticates a delivery."""
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}
    provisioned = _provision(client, owner)
    signed_body = json.dumps(_square_payload(quantity="7")).encode("utf-8")
    tampered_body = json.dumps(_square_payload(quantity="9999")).encode("utf-8")

    response = client.post(
        f"/connectors/webhook/{provisioned['endpoint_id']}",
        content=tampered_body,
        headers={
            "Content-Type": "application/json",
            "x-shelfwise-signature": _sign(provisioned["signing_secret"], signed_body),
        },
    )

    assert response.status_code == 401


def test_a_revoked_endpoint_stops_accepting_previously_valid_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}
    provisioned = _provision(client, owner)
    raw = json.dumps(_square_payload()).encode("utf-8")
    signature = _sign(provisioned["signing_secret"], raw)
    headers = {"Content-Type": "application/json", "x-shelfwise-signature": signature}

    delivery_url = f"/connectors/webhook/{provisioned['endpoint_id']}"
    before = client.post(delivery_url, content=raw, headers=headers)
    revoked = client.post(
        f"/connectors/webhook-endpoints/{provisioned['endpoint_id']}/revoke", headers=owner
    )
    after = client.post(delivery_url, content=raw, headers=headers)

    assert before.status_code == 200
    assert revoked.status_code == 200
    assert after.status_code == 401


def test_one_tenant_cannot_revoke_or_see_another_tenants_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    tenant_a = {"Authorization": f"Bearer {_token('owner', tenant_id='tenant_a')}"}
    tenant_b = {"Authorization": f"Bearer {_token('owner', tenant_id='tenant_b')}"}
    provisioned = _provision(client, tenant_a)

    b_list = client.get("/connectors/webhook-endpoints", headers=tenant_b)
    b_revoke = client.post(
        f"/connectors/webhook-endpoints/{provisioned['endpoint_id']}/revoke", headers=tenant_b
    )

    assert b_list.json()["endpoints"] == []
    assert b_revoke.status_code == 404


def test_a_delivery_is_attributed_to_the_endpoints_own_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated delivery must never be able to name the tenant it lands in."""
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    tenant_a = {"Authorization": f"Bearer {_token('owner', tenant_id='tenant_a')}"}
    provisioned = _provision(client, tenant_a)
    payload = _square_payload()
    payload["tenant_id"] = "tenant_b"
    raw = json.dumps(payload).encode("utf-8")

    response = client.post(
        f"/connectors/webhook/{provisioned['endpoint_id']}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-shelfwise-signature": _sign(provisioned["signing_secret"], raw),
        },
    )

    assert response.status_code == 200
    assert all(
        outcome["record"]["tenant_id"] == "tenant_a" for outcome in response.json()["records"]
    )


def test_unknown_endpoint_is_indistinguishable_from_a_bad_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    raw = json.dumps(_square_payload()).encode("utf-8")

    response = client.post(
        "/connectors/webhook/whep_does_not_exist",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-shelfwise-signature": _sign("anything", raw),
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid webhook signature"


def test_poll_based_systems_cannot_be_given_a_webhook_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Odoo has no webhook delivery path; offering one would imply a route that cannot work."""
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    owner = {"Authorization": f"Bearer {_token('owner')}"}

    response = client.post("/connectors/odoo/webhook-endpoint", json={}, headers=owner)

    assert response.status_code == 404


def test_provisioning_requires_the_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _jwt_mode(monkeypatch)
    client = TestClient(app)
    manager = {"Authorization": f"Bearer {_token('manager')}"}

    response = client.post("/connectors/square/webhook-endpoint", json={}, headers=manager)

    assert response.status_code == 403
