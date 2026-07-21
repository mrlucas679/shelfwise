from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, twin_service
from shelfwise_edge import EdgeDevice, edge_device_registry


def _body() -> bytes:
    """Build one derived, media-free edge batch."""
    return json.dumps(
        {
            "batch_id": "batch_edge_001",
            "tenant_id": "sa_retail_demo",
            "store_id": "store_edge_1",
            "device_id": "device_001",
            "sent_at": "2026-07-13T08:00:00Z",
            "observations": [
                {
                    "observation_id": "obs_edge_001",
                    "tenant_id": "sa_retail_demo",
                    "store_id": "store_edge_1",
                    "twin_id": "urn:shelfwise:sa_retail_demo:store_edge_1:fixture:fridge_1",
                    "property_name": "cold_chain.status",
                    "lane": "reported",
                    "value": "healthy",
                    "observed_at": "2026-07-13T08:00:00Z",
                    "source_system": "edge_device",
                    "source_object_id": "frame-derived-001",
                    "source_quality": 0.98,
                    "correlation_id": "cor-edge-001",
                    "payload_hash": "b" * 64,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_signed_edge_batch_projects_without_storing_media() -> None:
    secret = b"edge-secret-for-test"
    edge_device_registry.register(
        EdgeDevice(
            device_id="device_001",
            tenant_id="sa_retail_demo",
            store_id="store_edge_1",
            hmac_secret=secret,
        )
    )
    body = _body()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    client = TestClient(app)

    response = client.post(
        "/twin/edge/observations",
        content=body,
        headers={
            "content-type": "application/json",
            "x-shelfwise-device": "device_001",
            "x-shelfwise-signature": signature,
        },
    )
    replay = client.post(
        "/twin/edge/observations",
        content=body,
        headers={
            "content-type": "application/json",
            "x-shelfwise-device": "device_001",
            "x-shelfwise-signature": signature,
        },
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert replay.status_code == 202
    assert replay.json()["status"] == "duplicate"


def test_failed_edge_projection_releases_batch_for_a_signed_retry(monkeypatch) -> None:
    secret = b"edge-secret-for-test"
    edge_device_registry.register(
        EdgeDevice(
            device_id="device_001",
            tenant_id="sa_retail_demo",
            store_id="store_edge_1",
            hmac_secret=secret,
        )
    )
    body = _body()
    headers = {
        "content-type": "application/json",
        "x-shelfwise-device": "device_001",
        "x-shelfwise-signature": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
    }
    original_accept = twin_service.accept

    def fail_projection(_observation):
        raise RuntimeError("simulated projection failure")

    monkeypatch.setattr(twin_service, "accept", fail_projection)
    client = TestClient(app, raise_server_exceptions=False)
    failed = client.post("/twin/edge/observations", content=body, headers=headers)
    monkeypatch.setattr(twin_service, "accept", original_accept)
    retried = client.post("/twin/edge/observations", content=body, headers=headers)

    assert failed.status_code == 503
    assert "retry the same signed batch" in failed.json()["detail"]
    assert retried.status_code == 202
    assert retried.json()["status"] == "accepted"
    assert retried.json()["accepted"] == 1


def test_edge_gateway_rejects_bad_signature_before_parsing() -> None:
    edge_device_registry.register(
        EdgeDevice(
            device_id="device_001",
            tenant_id="sa_retail_demo",
            store_id="store_edge_1",
            hmac_secret=b"edge-secret-for-test",
        )
    )
    client = TestClient(app)
    response = client.post(
        "/twin/edge/observations",
        content=_body(),
        headers={
            "content-type": "application/json",
            "x-shelfwise-device": "device_001",
            "x-shelfwise-signature": "sha256=" + "0" * 64,
        },
    )
    assert response.status_code == 401
