from __future__ import annotations

import time

import pytest
from _world_test_support import demo_sku
from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
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
        "data_domain": "world_simulation",
        "payload": {"sku": demo_sku(), "location": "store_12"},
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

    assert default_tenant_context().tenant_id == "local"


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


def test_jwt_auth_mode_scopes_learning_to_the_authenticated_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    # Per-run unique tenants: against a persistent shared Postgres, fixed tenant ids make
    # the second run's identical ingest a correct duplicate (cascade None) and accumulate
    # learning rows across runs - rerun-safety requires fresh identity, not a fresh DB.
    from uuid import uuid4

    suffix = uuid4().hex[:10]
    tenant_a_id = f"tenant_a_{suffix}"
    tenant_b_id = f"tenant_b_{suffix}"
    tenant_a = {"Authorization": f"Bearer {_token('manager', tenant_id=tenant_a_id)}"}
    tenant_b = {"Authorization": f"Bearer {_token('manager', tenant_id=tenant_b_id)}"}

    unauthenticated = client.get("/learning")
    decision_a = client.post("/ingest", json=_scan_event(tenant_a_id), headers=tenant_a)
    decision_b = client.post("/ingest", json=_scan_event(tenant_b_id), headers=tenant_b)
    decision_a_id = decision_a.json()["cascade"]["decision"]["id"]
    decision_b_id = decision_b.json()["cascade"]["decision"]["id"]
    client.post(f"/decisions/{decision_a_id}/approve", headers=tenant_a)
    client.post(f"/decisions/{decision_b_id}/approve", headers=tenant_b)

    tenant_a_learning = client.get("/learning", headers=tenant_a)
    tenant_b_learning = client.get("/learning", headers=tenant_b)

    assert unauthenticated.status_code == 401
    assert tenant_a_learning.status_code == 200
    assert tenant_b_learning.status_code == 200
    assert all(
        event["tenant_id"] == tenant_a_id for event in tenant_a_learning.json()["events"]
    )
    assert all(
        event["tenant_id"] == tenant_b_id for event in tenant_b_learning.json()["events"]
    )
    assert {event["decision_id"] for event in tenant_a_learning.json()["events"]} == {decision_a_id}
    assert {event["decision_id"] for event in tenant_b_learning.json()["events"]} == {decision_b_id}


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

    golden = client.post("/scenarios/golden", headers=tenant)
    rejection = client.post("/scenarios/critic-rejection", headers=tenant)

    assert golden.status_code == 200
    assert golden.json()["decision"]["tenant_id"] == "tenant_demo"
    assert rejection.status_code == 200
    assert rejection.json()["decision"]["tenant_id"] == "tenant_demo"
    decisions = client.get("/decisions", headers=tenant).json()["decisions"]
    decision_ids = {item["id"] for item in decisions}
    assert golden.json()["decision"]["id"] in decision_ids
    assert rejection.json()["decision"]["id"] in decision_ids


def test_jwt_auth_mode_blocks_analysts_from_scenario_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario drills create events and decisions, so they require an ingest-capable role."""
    client = TestClient(app)
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    analyst = {"Authorization": f"Bearer {_token('analyst')}"}
    manager = {"Authorization": f"Bearer {_token('manager')}"}

    unauthenticated = client.post("/scenarios/golden")
    blocked = client.post("/scenarios/golden", headers=analyst)
    allowed = client.post("/scenarios/golden", headers=manager)
    decisions_before_preview = len(app_module.decision_store.list())
    preview = client.get("/scenarios/golden")

    assert unauthenticated.status_code == 401
    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["decision"]["tenant_id"] == "sa_retail_demo"
    assert preview.status_code == 200
    assert "decision" in preview.json()
    assert len(app_module.decision_store.list()) == decisions_before_preview


def test_public_demo_sessions_create_stable_isolated_browser_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    monkeypatch.setenv("SHELFWISE_PUBLIC_DEMO_SESSION", "true")
    monkeypatch.setenv("SHELFWISE_COOKIE_SECURE", "false")
    first = TestClient(app)
    second = TestClient(app)

    first_session = first.post("/auth/session")
    replayed_session = first.post("/auth/session")
    second_session = second.post("/auth/session")
    first_user = first_session.json()["session"]["user_id"]
    second_user = second_session.json()["session"]["user_id"]

    assert first_session.status_code == 200
    assert first_user == replayed_session.json()["session"]["user_id"]
    assert first_user != second_user
    assert first.post(
        "/chat",
        json={"question": "What needs attention?", "conversation_id": "shared"},
    ).status_code == 200
    assert first.get("/chat/conversations/shared").status_code == 200
    assert second.get("/chat/conversations/shared").status_code == 404


def test_public_demo_session_is_disabled_by_default_in_jwt_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    monkeypatch.delenv("SHELFWISE_PUBLIC_DEMO_SESSION", raising=False)

    assert TestClient(app).post("/auth/session").status_code == 401


def test_configured_frontend_origin_can_use_jwt_session_cookie() -> None:
    response = TestClient(app).options(
        "/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_bearer_header_is_used_for_storage_tenant_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    token = _token("manager", tenant_id="tenant_a")
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )

    assert app_module._tenant_id_from_request(request) == "tenant_a"


def test_company_login_mints_the_trusted_owner_session(monkeypatch) -> None:
    """Real credential verification (stdlib scrypt), honest 503 unconfigured, uniform
    401 on bad credentials, and the exact owner JWT cookie the platform already trusts."""
    import hashlib
    import os as _os

    client = TestClient(app)

    monkeypatch.delenv("SHELFWISE_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("SHELFWISE_LOGIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    unconfigured = client.post(
        "/auth/login", json={"email": "owner@shop.test", "password": "pw"}
    )
    assert unconfigured.status_code == 503, "unconfigured login must never be an open door"

    salt = _os.urandom(16)
    digest = hashlib.scrypt(b"correct-horse", salt=salt, n=16384, r=8, p=1)
    monkeypatch.setenv("SHELFWISE_LOGIN_EMAIL", "owner@shop.test")
    monkeypatch.setenv(
        "SHELFWISE_LOGIN_PASSWORD_HASH", f"scrypt${salt.hex()}${digest.hex()}"
    )

    wrong_pw = client.post(
        "/auth/login", json={"email": "owner@shop.test", "password": "wrong"}
    )
    wrong_email = client.post(
        "/auth/login", json={"email": "intruder@shop.test", "password": "correct-horse"}
    )
    assert wrong_pw.status_code == 401
    assert wrong_email.status_code == 401
    assert wrong_pw.json() == wrong_email.json(), "no oracle about which field was wrong"

    ok = client.post(
        "/auth/login", json={"email": "Owner@Shop.Test", "password": "correct-horse"}
    )
    assert ok.status_code == 200
    session = ok.json()["session"]
    assert session["role"] == "owner"
    assert session["user_id"] == "owner@shop.test"
    assert "shelfwise_session" in ok.headers.get("set-cookie", "").lower() or ok.cookies, (
        "login must set the same session cookie the platform verifies"
    )
