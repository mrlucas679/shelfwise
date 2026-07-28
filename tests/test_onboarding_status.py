from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.tenant import encode_hs256_token


def test_onboarding_status_starts_incomplete_without_inventing_progress() -> None:
    response = TestClient(app).get("/onboarding/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_operations"] is False
    assert body["required_steps"] == {"completed": 0, "total": 3, "next": "company"}
    assert body["company"] == {"configured": False, "name": ""}
    assert body["stores"] == []
    assert body["data"] == {
        "configured": False,
        "connector_systems": [],
        "has_imported_records": False,
    }


def test_onboarding_status_resumes_from_authoritative_server_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "onboarding-test-key")
    client = TestClient(app)

    assert client.post("/tenants/me", json={"name": "Kasi Grocer"}).status_code == 200
    assert client.post(
        "/twin/onboarding/self-service",
        json={
            "store_id": "kasi_grocer_soweto",
            "display_name": "Kasi Grocer Soweto",
            "entities": [
                {
                    "local_id": "dairy",
                    "entity_type": "area",
                    "display_name": "Dairy fridge",
                }
            ],
        },
    ).status_code == 200
    assert client.post(
        "/connectors/odoo/credentials",
        json={
            "fields": {
                "base_url": "https://example.invalid",
                "database": "kasi",
                "user_id": "7",
                "api_key": "not-a-real-key",
            }
        },
    ).status_code == 200

    response = client.get("/onboarding/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_operations"] is True
    assert body["required_steps"] == {"completed": 3, "total": 3, "next": "review"}
    assert body["company"] == {"configured": True, "name": "Kasi Grocer"}
    assert body["stores"] == [
        {
            "store_id": "kasi_grocer_soweto",
            "display_name": "Kasi Grocer Soweto",
            "timezone": "Africa/Johannesburg",
            "entity_count": 2,
        }
    ]
    assert body["data"] == {
        "configured": True,
        "connector_systems": ["odoo"],
        "has_imported_records": False,
    }


def test_onboarding_status_treats_csv_as_a_real_data_source() -> None:
    client = TestClient(app)
    assert client.post("/tenants/me", json={"name": "CSV Shop"}).status_code == 200
    assert client.post(
        "/twin/onboarding/self-service",
        json={"store_id": "csv_shop", "display_name": "CSV Shop"},
    ).status_code == 200
    csv_text = "sku,name,barcode\nSKU-1,Milk,6001000000001\n"

    committed = client.post(
        "/intake/csv/commit",
        json={"kind": "products", "csv_text": csv_text},
    )
    response = client.get("/onboarding/status")

    assert committed.status_code == 200
    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": True,
        "connector_systems": [],
        "has_imported_records": True,
    }
    assert response.json()["ready_for_operations"] is True


def test_onboarding_status_is_owner_only_in_jwt_mode(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "onboarding-auth-test-secret")
    manager_token = encode_hs256_token(
        {
            "tenant_id": "sa_retail_demo",
            "user_id": "manager_1",
            "role": "manager",
        },
        secret="onboarding-auth-test-secret",
    )
    owner_token = encode_hs256_token(
        {
            "tenant_id": "sa_retail_demo",
            "user_id": "owner_1",
            "role": "owner",
        },
        secret="onboarding-auth-test-secret",
    )
    client = TestClient(app)

    blocked = client.get(
        "/onboarding/status",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    allowed = client.get(
        "/onboarding/status",
        headers={"Authorization": f"Bearer {owner_token}"},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_onboarding_status_does_not_leak_another_tenants_progress(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "onboarding-tenant-test-secret")

    def owner_headers(tenant_id: str) -> dict[str, str]:
        token = encode_hs256_token(
            {
                "tenant_id": tenant_id,
                "user_id": f"{tenant_id}_owner",
                "role": "owner",
            },
            secret="onboarding-tenant-test-secret",
        )
        return {"Authorization": f"Bearer {token}"}

    client = TestClient(app)
    tenant_a = owner_headers("tenant_a")
    tenant_b = owner_headers("tenant_b")
    profile_response = client.post(
        "/tenants/me",
        json={"name": "Tenant A Shop"},
        headers=tenant_a,
    )
    assert client.post(
        "/twin/onboarding/self-service",
        json={"store_id": "tenant_a_store", "display_name": "Tenant A Store"},
        headers=tenant_a,
    ).status_code == 200
    assert profile_response.status_code == 200

    first_status = client.get("/onboarding/status", headers=tenant_a)
    other_status = client.get("/onboarding/status", headers=tenant_b)

    assert first_status.json()["company"]["name"] == "Tenant A Shop"
    assert first_status.json()["stores"][0]["store_id"] == "tenant_a_store"
    assert other_status.status_code == 200
    assert other_status.json()["company"] == {"configured": False, "name": ""}
    assert other_status.json()["stores"] == []
