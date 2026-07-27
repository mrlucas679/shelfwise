from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_edge import edge_device_registry


def _owner_headers() -> dict[str, str]:
    token = encode_hs256_token(
        {"tenant_id": "sa_retail_demo", "user_id": "owner_1", "role": "owner"},
        secret="test-secret",
    )
    return {"Authorization": f"Bearer {token}"}


def _owner_headers_for(tenant_id: str) -> dict[str, str]:
    token = encode_hs256_token(
        {"tenant_id": tenant_id, "user_id": "owner_1", "role": "owner"},
        secret="test-secret",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _enable_jwt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    edge_device_registry.clear()
    yield
    edge_device_registry.clear()


def test_registering_a_device_returns_the_secret_exactly_once() -> None:
    client = TestClient(app)
    response = client.post(
        "/twin/stores/store_1/devices",
        headers=_owner_headers(),
        json={},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["device_id"]
    assert len(payload["hmac_secret"]) == 64  # 32 raw bytes, hex-encoded
    assert "shown once" in payload["warning"]


def test_registered_device_never_appears_with_its_secret_in_the_list_endpoint() -> None:
    client = TestClient(app)
    register = client.post("/twin/stores/store_1/devices", headers=_owner_headers(), json={})
    device_id = register.json()["device_id"]
    secret = register.json()["hmac_secret"]

    listing = client.get("/twin/stores/store_1/devices", headers=_owner_headers())
    assert listing.status_code == 200
    body_text = listing.text
    assert secret not in body_text
    assert device_id in body_text


def test_a_freshly_registered_device_can_sign_a_real_edge_batch() -> None:
    """Proves the actual "connect a camera" path end to end: register a device via the
    owner-facing API, then use exactly the credential it returned to authenticate a real
    signed observation batch - no other provisioning step exists or is needed."""
    client = TestClient(app)
    register = client.post("/twin/stores/store_1/devices", headers=_owner_headers(), json={})
    device_id = register.json()["device_id"]
    secret = bytes.fromhex(register.json()["hmac_secret"])

    body = json.dumps(
        {
            "batch_id": "batch_reg_001",
            "tenant_id": "sa_retail_demo",
            "store_id": "store_1",
            "device_id": device_id,
            "sent_at": "2026-07-23T08:00:00Z",
            "observations": [
                {
                    "observation_id": "obs_reg_001",
                    "tenant_id": "sa_retail_demo",
                    "store_id": "store_1",
                    "twin_id": "urn:shelfwise:sa_retail_demo:store_1:fixture:fridge_1",
                    "property_name": "cold_chain.status",
                    "lane": "reported",
                    "value": "healthy",
                    "observed_at": "2026-07-23T08:00:00Z",
                    "source_system": "edge_device",
                    "source_object_id": "frame-derived-reg-001",
                    "source_quality": 0.98,
                    "correlation_id": "cor-edge-reg-001",
                    "payload_hash": "c" * 64,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    response = client.post(
        "/twin/edge/observations",
        content=body,
        headers={
            "content-type": "application/json",
            "x-shelfwise-device": device_id,
            "x-shelfwise-signature": signature,
        },
    )
    assert response.status_code == 202


def test_revoking_a_device_blocks_further_signed_batches() -> None:
    client = TestClient(app)
    register = client.post("/twin/stores/store_1/devices", headers=_owner_headers(), json={})
    device_id = register.json()["device_id"]
    secret = bytes.fromhex(register.json()["hmac_secret"])

    revoke = client.post(
        f"/twin/stores/store_1/devices/{device_id}/revoke", headers=_owner_headers()
    )
    assert revoke.status_code == 200
    assert revoke.json()["active"] is False

    body = json.dumps(
        {
            "batch_id": "batch_revoked_001",
            "tenant_id": "sa_retail_demo",
            "store_id": "store_1",
            "device_id": device_id,
            "sent_at": "2026-07-23T08:00:00Z",
            "observations": [],
        },
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    response = client.post(
        "/twin/edge/observations",
        content=body,
        headers={
            "content-type": "application/json",
            "x-shelfwise-device": device_id,
            "x-shelfwise-signature": signature,
        },
    )
    assert response.status_code == 401


def test_revoking_a_device_from_another_tenant_or_store_is_rejected() -> None:
    client = TestClient(app)
    register = client.post("/twin/stores/store_1/devices", headers=_owner_headers(), json={})
    device_id = register.json()["device_id"]

    wrong_store = client.post(
        f"/twin/stores/store_other/devices/{device_id}/revoke", headers=_owner_headers()
    )
    assert wrong_store.status_code == 404


def test_registration_requires_owner_role() -> None:
    manager_token = encode_hs256_token(
        {"tenant_id": "sa_retail_demo", "user_id": "manager_1", "role": "manager"},
        secret="test-secret",
    )
    client = TestClient(app)
    response = client.post(
        "/twin/stores/store_1/devices",
        headers={"Authorization": f"Bearer {manager_token}"},
        json={},
    )
    assert response.status_code == 403


def test_registration_fails_safely_when_durable_secret_encryption_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production provisioning must not leak a registry stack trace or store plaintext."""
    from shelfwise_backend import routes_twin
    from shelfwise_edge import EdgeDeviceRegistrationError

    class UnavailableRegistry:
        def provision(self, _device: object) -> None:
            raise EdgeDeviceRegistrationError("missing credential encryption key")

    monkeypatch.setattr(routes_twin, "edge_device_registry", UnavailableRegistry())

    response = TestClient(app).post(
        "/twin/stores/store_1/devices", headers=_owner_headers(), json={}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Device provisioning is not configured"


def test_registration_cannot_overwrite_another_tenants_device_id() -> None:
    client = TestClient(app)
    first = client.post(
        "/twin/stores/store_1/devices",
        headers=_owner_headers_for("tenant_a"),
        json={"device_id": "edge_shared_id"},
    )
    assert first.status_code == 200

    overwrite = client.post(
        "/twin/stores/store_2/devices",
        headers=_owner_headers_for("tenant_b"),
        json={"device_id": "edge_shared_id"},
    )
    assert overwrite.status_code == 422

    original = edge_device_registry.get_active("edge_shared_id")
    assert original is not None
    assert original.tenant_id == "tenant_a"
    assert original.store_id == "store_1"
