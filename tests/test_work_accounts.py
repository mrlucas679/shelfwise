from fastapi.testclient import TestClient

from shelfwise_backend.account_tokens import token_digest
from shelfwise_backend.app import account_store, app
from shelfwise_backend.tenant import encode_hs256_token


def test_owner_can_create_a_work_account_and_staff_can_log_in(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "off")
    client = TestClient(app)
    created = client.post(
        "/accounts",
        json={
            "email": "floor.manager@example.test", "given_name": "Amina",
            "surname": "Dlamini", "position": "Floor Manager", "role": "manager",
            "password": "a-safe-work-password",
        },
    )
    assert created.status_code == 200
    assert "password_hash" not in created.text
    assert created.json()["account"]["position"] == "Floor Manager"

    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    login = client.post(
        "/auth/login",
        json={"email": "floor.manager@example.test", "password": "a-safe-work-password"},
    )
    assert login.status_code == 200
    assert login.json()["session"]["role"] == "manager"
    assert login.json()["session"]["must_change_password"] is True


def test_owner_cannot_create_another_owner() -> None:
    response = TestClient(app).post(
        "/accounts",
        json={
            "email": "other.owner@example.test", "given_name": "Other", "surname": "Owner",
            "position": "Owner", "role": "owner", "password": "a-safe-work-password",
        },
    )
    assert response.status_code == 422


def test_owner_can_deactivate_a_work_account(monkeypatch) -> None:
    client = TestClient(app)
    created = client.post(
        "/accounts",
        json={
            "email": "inactive@example.test", "given_name": "Ina", "surname": "Ctive",
            "position": "Analyst", "role": "analyst", "password": "a-safe-work-password",
        },
    )
    response = client.post(f"/accounts/{created.json()['account']['id']}/deactivate")
    assert response.status_code == 200
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    login = client.post(
        "/auth/login",
        json={"email": "inactive@example.test", "password": "a-safe-work-password"},
    )
    assert login.status_code == 401
    owner_token = encode_hs256_token(
        {"tenant_id": "sa_retail_demo", "user_id": "owner_1", "role": "owner"},
        secret="test-secret",
    )
    response = client.post(
        f"/accounts/{created.json()['account']['id']}/reactivate",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert response.status_code == 200
    assert response.json()["account"]["active"] is True


def test_owner_can_change_a_staff_role() -> None:
    client = TestClient(app)
    created = client.post(
        "/accounts",
        json={
            "email": "role.change@example.test", "given_name": "Role", "surname": "Change",
            "position": "Inventory Assistant", "role": "inventory",
            "password": "a-safe-work-password",
        },
    )
    account_id = created.json()["account"]["id"]
    response = client.post(f"/accounts/{account_id}/role", json={"role": "manager"})
    assert response.status_code == 200
    assert response.json()["account"]["role"] == "manager"


def _configure_account_email(monkeypatch, captured: list[dict[str, str]]) -> None:
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("SHELFWISE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SHELFWISE_SMTP_FROM", "accounts@example.test")
    monkeypatch.setenv("SHELFWISE_PUBLIC_APP_URL", "https://shelfwise.example.test")

    def capture_link(**message: str) -> None:
        captured.append(message)

    monkeypatch.setattr(
        "shelfwise_backend.account_notifications.send_account_link",
        capture_link,
    )


def test_platform_bootstrap_is_one_time_and_never_exposes_password_hash(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("SHELFWISE_PLATFORM_BOOTSTRAP_KEY", "platform-bootstrap-secret")
    payload = {
        "company_name": "Ubuntu Foods",
        "email": "owner@ubuntu.test",
        "given_name": "Naledi",
        "surname": "Mokoena",
        "position": "Business Owner",
        "password": "a-safe-owner-password",
        "password_confirmation": "a-safe-owner-password",
    }
    client = TestClient(app)

    assert client.get("/auth/setup-status").json() == {"bootstrap_required": True}
    response = client.post(
        "/platform/bootstrap",
        headers={"x-bootstrap-key": "platform-bootstrap-secret"},
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["session"]["role"] == "owner"
    assert "password_hash" not in response.text
    assert client.get("/auth/setup-status").json() == {"bootstrap_required": False}
    assert client.post(
        "/platform/bootstrap",
        headers={"x-bootstrap-key": "platform-bootstrap-secret"},
        json=payload,
    ).status_code == 409


def test_invitation_activation_is_single_use_and_identity_bound(monkeypatch) -> None:
    captured: list[dict[str, str]] = []
    _configure_account_email(monkeypatch, captured)
    client = TestClient(app)
    invitation = {
        "email": "receiver@example.test",
        "given_name": "Thandi",
        "surname": "Ndlovu",
        "position": "Receiving Supervisor",
        "role": "inventory",
    }
    invited = client.post("/accounts/invitations", json=invitation)
    assert invited.status_code == 200
    assert invited.json()["account"]["status"] == "invited"
    assert "token" not in invited.text
    assert captured and captured[0]["purpose"] == "activate"

    activation = {
        **{key: value for key, value in invitation.items() if key != "role"},
        "token": captured[0]["token"],
        "password": "a-new-work-password",
        "password_confirmation": "a-new-work-password",
    }
    activated = client.post("/auth/activate", json=activation)
    assert activated.status_code == 200
    assert activated.json()["session"]["role"] == "inventory"
    assert client.post("/auth/activate", json=activation).status_code == 400


def test_invitation_rejects_changed_identity(monkeypatch) -> None:
    captured: list[dict[str, str]] = []
    _configure_account_email(monkeypatch, captured)
    client = TestClient(app)
    invitation = {
        "email": "analyst@example.test",
        "given_name": "Lerato",
        "surname": "Khumalo",
        "position": "Demand Analyst",
        "role": "analyst",
    }
    assert client.post("/accounts/invitations", json=invitation).status_code == 200
    activation = {
        **{key: value for key, value in invitation.items() if key != "role"},
        "given_name": "Someone Else",
        "token": captured[0]["token"],
        "password": "a-new-work-password",
        "password_confirmation": "a-new-work-password",
    }
    assert client.post("/auth/activate", json=activation).status_code == 400


def test_expired_invitation_is_rejected(monkeypatch) -> None:
    captured: list[dict[str, str]] = []
    _configure_account_email(monkeypatch, captured)
    client = TestClient(app)
    invitation = {
        "email": "expired@example.test",
        "given_name": "Expired",
        "surname": "Invite",
        "position": "Cashier",
        "role": "inventory",
    }
    invited = client.post("/accounts/invitations", json=invitation)
    account_id = invited.json()["account"]["id"]
    account_store.set_invitation(
        "sa_retail_demo",
        account_id,
        token_hash=token_digest(captured[0]["token"]),
        expires_at="2000-01-01T00:00:00+00:00",
    )
    activation = {
        **{key: value for key, value in invitation.items() if key != "role"},
        "token": captured[0]["token"],
        "password": "a-new-work-password",
        "password_confirmation": "a-new-work-password",
    }
    assert client.post("/auth/activate", json=activation).status_code == 400


def test_password_reset_invalidates_previously_issued_session(monkeypatch) -> None:
    captured: list[dict[str, str]] = []
    _configure_account_email(monkeypatch, captured)
    client = TestClient(app)
    created = client.post(
        "/accounts",
        json={
            "email": "auditor@example.test",
            "given_name": "Sipho",
            "surname": "Dube",
            "position": "Loss Prevention Auditor",
            "role": "auditor",
            "password": "original-work-password",
        },
    )
    account_id = created.json()["account"]["id"]
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    login = client.post(
        "/auth/login",
        json={"email": "auditor@example.test", "password": "original-work-password"},
    )
    old_token = login.cookies.get("shelfwise_session")
    assert old_token

    requested = client.post(
        "/auth/password-reset/request",
        json={"email": "auditor@example.test"},
    )
    assert requested.json() == {"status": "accepted"}
    reset = client.post(
        "/auth/password-reset/consume",
        json={
            "token": captured[-1]["token"],
            "password": "replacement-work-password",
            "password_confirmation": "replacement-work-password",
        },
    )
    assert reset.status_code == 200
    assert client.get(
        "/readiness",
        headers={"Authorization": f"Bearer {old_token}"},
    ).status_code == 401
    assert account_store.get_by_id("sa_retail_demo", account_id)["reset_token_hash"] is None


def test_forced_password_change_replaces_temporary_session(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    client = TestClient(app)
    client.post(
        "/accounts",
        json={
            "email": "temporary@example.test",
            "given_name": "Temp",
            "surname": "Worker",
            "position": "Till Supervisor",
            "role": "manager",
            "password": "temporary-work-password",
        },
    )
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    login = client.post(
        "/auth/login",
        json={"email": "temporary@example.test", "password": "temporary-work-password"},
    )
    old_token = login.cookies.get("shelfwise_session")
    assert login.json()["session"]["must_change_password"] is True
    resumed = client.post(
        "/auth/session",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert resumed.json()["session"]["must_change_password"] is True
    assert client.get(
        "/readiness",
        headers={"Authorization": f"Bearer {old_token}"},
    ).status_code == 403

    changed = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {old_token}"},
        json={
            "current_password": "temporary-work-password",
            "password": "permanent-work-password",
            "password_confirmation": "permanent-work-password",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["session"]["must_change_password"] is False
    assert client.get(
        "/readiness",
        headers={"Authorization": f"Bearer {old_token}"},
    ).status_code == 401


def test_owner_cannot_deactivate_self_and_manager_cannot_manage_accounts(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_AUTH_SECRET", "test-secret")
    owner = account_store.create_first_owner(
        {
            "tenant_id": "sa_retail_demo",
            "email": "owner@example.test",
            "given_name": "Owner",
            "surname": "Account",
            "position": "Business Owner",
            "role": "owner",
            "password_hash": "not-used",
        }
    )
    manager = account_store.create(
        {
            "tenant_id": "sa_retail_demo",
            "email": "manager@example.test",
            "given_name": "Store",
            "surname": "Manager",
            "position": "Store Manager",
            "role": "manager",
            "password_hash": "not-used",
        }
    )
    client = TestClient(app)
    owner_token = encode_hs256_token(
        {"tenant_id": "sa_retail_demo", "user_id": owner["id"], "role": "owner"},
        secret="test-secret",
    )
    manager_token = encode_hs256_token(
        {"tenant_id": "sa_retail_demo", "user_id": manager["id"], "role": "manager"},
        secret="test-secret",
    )
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    assert client.post(
        f"/accounts/{owner['id']}/deactivate",
        headers={"Authorization": f"Bearer {owner_token}"},
    ).status_code == 409
    assert client.get(
        "/accounts",
        headers={"Authorization": f"Bearer {manager_token}"},
    ).status_code == 403


def test_invitation_fails_before_account_creation_when_email_is_unconfigured(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SHELFWISE_SMTP_HOST", raising=False)
    response = TestClient(app).post(
        "/accounts/invitations",
        json={
            "email": "not-created@example.test",
            "given_name": "Not",
            "surname": "Created",
            "position": "Cashier",
            "role": "inventory",
        },
    )
    assert response.status_code == 503
    assert account_store.get_by_email("sa_retail_demo", "not-created@example.test") is None
