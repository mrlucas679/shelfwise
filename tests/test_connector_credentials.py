from __future__ import annotations

import pytest

from shelfwise_connectors import (
    ConnectorCredentialError,
    CredentialEncryptionError,
    InMemoryConnectorCredentialStore,
    SourceSystem,
    decrypt_credential_fields,
    encrypt_credential_fields,
    resolve_connector_credentials,
)


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "test-secret-key-do-not-use")


def test_encrypt_decrypt_round_trips_exactly() -> None:
    fields = {"base_url": "https://odoo.example", "api_key": "sk_live_abc123"}
    token = encrypt_credential_fields(fields)

    assert token != str(fields)
    assert "sk_live_abc123" not in token
    assert decrypt_credential_fields(token) == fields


def test_encryption_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(CredentialEncryptionError, match="SHELFWISE_CREDENTIAL_ENCRYPTION_KEY"):
        encrypt_credential_fields({"api_key": "x"})


def test_decrypting_with_the_wrong_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encrypt_credential_fields({"api_key": "secret"})
    monkeypatch.setenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "a-different-secret")
    with pytest.raises(CredentialEncryptionError, match="could not be decrypted"):
        decrypt_credential_fields(token)


def test_store_rejects_empty_tenant_or_empty_fields() -> None:
    store = InMemoryConnectorCredentialStore()
    with pytest.raises(ConnectorCredentialError, match="tenant_id"):
        store.upsert(tenant_id="", system=SourceSystem.ODOO, fields={"api_key": "x"})
    with pytest.raises(ConnectorCredentialError, match="fields must not be empty"):
        store.upsert(tenant_id="t1", system=SourceSystem.ODOO, fields={})
    with pytest.raises(ConnectorCredentialError, match="must not be blank"):
        store.upsert(tenant_id="t1", system=SourceSystem.ODOO, fields={"api_key": "  "})


def test_store_is_isolated_per_tenant_and_never_leaks_plaintext_across_lookups() -> None:
    store = InMemoryConnectorCredentialStore()
    store.upsert(
        tenant_id="tenant_a",
        system=SourceSystem.ODOO,
        fields={"api_key": "tenant-a-secret"},
    )
    store.upsert(
        tenant_id="tenant_b",
        system=SourceSystem.ODOO,
        fields={"api_key": "tenant-b-secret"},
    )

    assert store.get(tenant_id="tenant_a", system=SourceSystem.ODOO) == {
        "api_key": "tenant-a-secret"
    }
    assert store.get(tenant_id="tenant_b", system=SourceSystem.ODOO) == {
        "api_key": "tenant-b-secret"
    }
    assert store.get(tenant_id="tenant_c", system=SourceSystem.ODOO) is None


def test_store_upsert_overwrites_and_delete_removes() -> None:
    store = InMemoryConnectorCredentialStore()
    store.upsert(tenant_id="t1", system=SourceSystem.SAP, fields={"token": "v1"})
    store.upsert(tenant_id="t1", system=SourceSystem.SAP, fields={"token": "v2"})
    assert store.get(tenant_id="t1", system=SourceSystem.SAP) == {"token": "v2"}

    store.delete(tenant_id="t1", system=SourceSystem.SAP)
    assert store.get(tenant_id="t1", system=SourceSystem.SAP) is None


def test_list_configured_systems_reflects_only_that_tenants_writes() -> None:
    store = InMemoryConnectorCredentialStore()
    store.upsert(tenant_id="t1", system=SourceSystem.ODOO, fields={"api_key": "a"})
    store.upsert(tenant_id="t1", system=SourceSystem.SAP, fields={"token": "b"})
    store.upsert(tenant_id="t2", system=SourceSystem.SYSPRO, fields={"token": "c"})

    assert set(store.list_configured_systems(tenant_id="t1")) == {
        SourceSystem.ODOO,
        SourceSystem.SAP,
    }
    assert set(store.list_configured_systems(tenant_id="t2")) == {SourceSystem.SYSPRO}


def test_resolve_prefers_stored_credentials_over_env_fallback() -> None:
    store = InMemoryConnectorCredentialStore()
    store.upsert(
        tenant_id="t1", system=SourceSystem.ODOO, fields={"api_key": "tenant-specific"}
    )

    resolved = resolve_connector_credentials(
        store,
        tenant_id="t1",
        system=SourceSystem.ODOO,
        env_fallback={"api_key": "global-env-default"},
    )

    assert resolved == {"api_key": "tenant-specific"}


def test_resolve_falls_back_to_env_when_tenant_has_no_stored_credentials() -> None:
    store = InMemoryConnectorCredentialStore()

    resolved = resolve_connector_credentials(
        store,
        tenant_id="t1",
        system=SourceSystem.ODOO,
        env_fallback={"api_key": "global-env-default"},
    )

    assert resolved == {"api_key": "global-env-default"}
