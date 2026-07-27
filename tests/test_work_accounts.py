from fastapi.testclient import TestClient

from shelfwise_backend.app import app
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
