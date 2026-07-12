from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.tenant import (
    default_tenant_context,
    encode_hs256_token,
    tenant_context_from_jwt,
)


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


def _scan_event(tenant_id: str = "sa_retail_demo") -> dict[str, object]:
    return {
        "id": f"evt_auth_{tenant_id}",
        "type": "scan",
        "ts": "2026-07-06T10:14:00Z",
        "actor": "store_12",
        "source": "scanner",
        "tenant_id": tenant_id,
        "payload": {"sku": "4011", "location": "store_12"},
    }


def test_tenant_jwt_verifies_signature_and_claims() -> None:
    token = _token("manager")

    ctx = tenant_context_from_jwt(token, secret="secret")

    assert ctx.tenant_id == "sa_retail_demo"
    assert ctx.user_id == "user_1"
    assert ctx.role.value == "manager"


def test_default_tenant_context_matches_demo_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELFWISE_TENANT_ID", raising=False)
    monkeypatch.delenv("TENANT_ID", raising=False)

    assert default_tenant_context().tenant_id == "sa_retail_demo"


def test_tenant_jwt_rejects_bad_signature_and_expired_token() -> None:
    expired = encode_hs256_token(
        {
            "tenant_id": "sa_retail_demo",
            "user_id": "user_1",
            "role": "manager",
            "exp": int(time.time()) - 1,
        },
        secret="secret",
    )

    with pytest.raises(ValueError, match="invalid token signature"):
        tenant_context_from_jwt(_token("manager"), secret="wrong")
    with pytest.raises(ValueError, match="token expired"):
        tenant_context_from_jwt(expired, secret="secret")


def test_jwt_auth_mode_guards_ingest_tenant_and_approval_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    manager = {"Authorization": f"Bearer {_token('manager')}"}
    analyst = {"Authorization": f"Bearer {_token('analyst')}"}
    other_tenant = {"Authorization": f"Bearer {_token('manager', tenant_id='other')}"}

    no_token = client.post("/ingest", json=_scan_event())
    wrong_role = client.post("/ingest", json=_scan_event(), headers=analyst)
    tenant_mismatch = client.post("/ingest", json=_scan_event(), headers=other_tenant)
    allowed = client.post("/ingest", json=_scan_event(), headers=manager)
    decision_id = allowed.json()["cascade"]["decision"]["id"]
    blocked_approval = client.post(f"/decisions/{decision_id}/approve", headers=analyst)
    allowed_approval = client.post(f"/decisions/{decision_id}/approve", headers=manager)

    assert no_token.status_code == 401
    assert wrong_role.status_code == 403
    assert tenant_mismatch.status_code == 403
    assert allowed.status_code == 200
    assert blocked_approval.status_code == 403
    assert allowed_approval.status_code == 200
    assert allowed_approval.json()["decision"]["status"] == "approved"


def test_jwt_auth_mode_blocks_cross_tenant_decision_read_approve_and_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid manager token for tenant B must not read, approve, or reject a decision
    that belongs to tenant A - not even to learn that it exists (IDOR)."""
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    tenant_a_manager = {"Authorization": f"Bearer {_token('manager', tenant_id='sa_retail_demo')}"}
    tenant_b_manager = {"Authorization": f"Bearer {_token('manager', tenant_id='other_tenant')}"}

    created = client.post("/ingest", json=_scan_event(), headers=tenant_a_manager)
    decision_id = created.json()["cascade"]["decision"]["id"]

    cross_tenant_read = client.get(f"/decisions/{decision_id}", headers=tenant_b_manager)
    cross_tenant_approve = client.post(
        f"/decisions/{decision_id}/approve", headers=tenant_b_manager
    )
    cross_tenant_reject = client.post(f"/decisions/{decision_id}/reject", headers=tenant_b_manager)
    owner_read = client.get(f"/decisions/{decision_id}", headers=tenant_a_manager)

    assert created.status_code == 200
    assert cross_tenant_read.status_code == 404
    assert cross_tenant_approve.status_code == 404
    assert cross_tenant_reject.status_code == 404
    assert owner_read.status_code == 200
    assert owner_read.json()["decision"]["id"] == decision_id


def test_jwt_auth_mode_scopes_decision_list_to_the_authenticated_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    tenant_a_manager = {"Authorization": f"Bearer {_token('manager', tenant_id='sa_retail_demo')}"}
    tenant_b_manager = {"Authorization": f"Bearer {_token('manager', tenant_id='other_tenant')}"}

    client.post("/ingest", json=_scan_event(), headers=tenant_a_manager)

    tenant_a_list = client.get("/decisions", headers=tenant_a_manager)
    tenant_b_list = client.get("/decisions", headers=tenant_b_manager)

    assert tenant_a_list.status_code == 200
    assert len(tenant_a_list.json()["decisions"]) >= 1
    assert tenant_b_list.status_code == 200
    assert tenant_b_list.json()["decisions"] == []


def test_jwt_auth_mode_scopes_traces_to_the_authenticated_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    tenant_a = {"Authorization": f"Bearer {_token('manager', tenant_id='sa_retail_demo')}"}
    tenant_b = {"Authorization": f"Bearer {_token('manager', tenant_id='other_tenant')}"}

    created = client.post("/ingest", json=_scan_event(), headers=tenant_a)
    correlation_id = created.json()["cascade"]["correlation_id"]

    assert client.get(f"/trace/{correlation_id}", headers=tenant_a).status_code == 200
    assert client.get(f"/trace/{correlation_id}", headers=tenant_b).status_code == 404
    tenant_b_traces = client.get("/traces", headers=tenant_b).json()["traces"]
    assert all(item["correlation_id"] != correlation_id for item in tenant_b_traces)


def test_jwt_auth_mode_assigns_demo_outputs_to_authenticated_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    tenant = {"Authorization": f"Bearer {_token('manager', tenant_id='tenant_demo')}"}

    golden = client.post("/demo/golden", headers=tenant)
    rejection = client.post("/demo/critic-rejection", headers=tenant)

    assert golden.status_code == 200
    assert golden.json()["decision"]["tenant_id"] == "tenant_demo"
    assert rejection.status_code == 200
    assert rejection.json()["decision"]["tenant_id"] == "tenant_demo"
    decisions = client.get("/decisions", headers=tenant).json()["decisions"]
    decision_ids = {item["id"] for item in decisions}
    assert golden.json()["decision"]["id"] in decision_ids
    assert rejection.json()["decision"]["id"] in decision_ids
