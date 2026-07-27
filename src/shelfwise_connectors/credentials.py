"""Real, encrypted, tenant-scoped connector credential storage.

Closes the gap the 2026-07-14 connector-poll work explicitly scoped out: that work read
Odoo/SAP/SYSPRO/Dynamics credentials from process-wide environment variables, correct for the
single deployment running today but structurally unable to give two tenants different ERP
credentials. This module is the real fix - encrypted at rest (Fernet, keyed from
`SHELFWISE_CREDENTIAL_ENCRYPTION_KEY`, never plaintext in the database), tenant-isolated via
the same RLS pattern every other tenant-scoped table in this codebase uses, with real CRUD.

Deliberately does NOT change `connector_poll_service.py`'s single background loop into a
per-tenant polling loop - that is a separate, larger architectural change (one poll task per
tenant, concurrency/backpressure budget, partial-failure isolation between tenants) that
deserves its own dedicated design pass, not a rider on a credential-storage fix. What this
module does provide: `resolve_connector_credentials`, which prefers a tenant's stored
credentials over the global env-var fallback wherever connector configuration is resolved -
so a tenant that configures credentials via the new API immediately gets tenant-specific
behavior in every credential-resolution call site, without the poll loop's iteration model
needing to change first.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from threading import Lock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from shelfwise_storage import auto_schema_enabled, connect
from shelfwise_storage.rls import apply_tenant_rls

from .canonical import SourceSystem


class CredentialEncryptionError(RuntimeError):
    """Raised when credentials cannot be encrypted or decrypted."""


class ConnectorCredentialError(ValueError):
    """Raised for an invalid credential write (empty tenant, empty field set, etc.)."""


def _encryption_key() -> bytes:
    """Derive a Fernet key from `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY`.

    Fernet requires a 32-byte urlsafe-base64 key. Rather than force every deployment to
    generate and store one in exactly that format, accept any non-empty secret string and
    derive a valid Fernet key from it via SHA-256 - the same "any secret in, one valid key
    out" approach `TENANT_AUTH_SECRET`/JWT signing already uses elsewhere in this codebase,
    so operators configure one new env var the same way they configure the others.
    """
    raw = os.getenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise CredentialEncryptionError(
            "SHELFWISE_CREDENTIAL_ENCRYPTION_KEY must be set to store or read connector "
            "credentials - refusing to fall back to an unkeyed or hardcoded default for "
            "data this sensitive"
        )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_credential_fields(fields: dict[str, str]) -> str:
    """Encrypt a credential field map into one opaque token for storage."""
    if not fields:
        raise ConnectorCredentialError("credential fields must not be empty")
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Fernet(_encryption_key()).encrypt(payload).decode("ascii")


def decrypt_credential_fields(token: str) -> dict[str, str]:
    """Decrypt a stored token back into its credential field map."""
    try:
        payload = Fernet(_encryption_key()).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise CredentialEncryptionError(
            "stored credential could not be decrypted - wrong encryption key or corrupted "
            "data"
        ) from exc
    return dict(json.loads(payload))


def _validate_write(tenant_id: str, system: SourceSystem, fields: dict[str, str]) -> None:
    if not tenant_id.strip():
        raise ConnectorCredentialError("tenant_id is required")
    if not fields:
        raise ConnectorCredentialError("credential fields must not be empty")
    if any(not str(value).strip() for value in fields.values()):
        raise ConnectorCredentialError("credential field values must not be blank")


class InMemoryConnectorCredentialStore:
    """Tenant-isolated in-memory credential store for local development and tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._credentials: dict[tuple[str, str], str] = {}

    def upsert(
        self, *, tenant_id: str, system: SourceSystem, fields: dict[str, str]
    ) -> None:
        _validate_write(tenant_id, system, fields)
        token = encrypt_credential_fields(fields)
        with self._lock:
            self._credentials[(tenant_id, system.value)] = token

    def get(self, *, tenant_id: str, system: SourceSystem) -> dict[str, str] | None:
        with self._lock:
            token = self._credentials.get((tenant_id, system.value))
        return decrypt_credential_fields(token) if token is not None else None

    def delete(self, *, tenant_id: str, system: SourceSystem) -> None:
        with self._lock:
            self._credentials.pop((tenant_id, system.value), None)

    def list_configured_systems(self, *, tenant_id: str) -> tuple[SourceSystem, ...]:
        with self._lock:
            return tuple(
                SourceSystem(system)
                for (tid, system) in self._credentials
                if tid == tenant_id
            )

    def clear(self) -> None:
        with self._lock:
            self._credentials.clear()


_CREDENTIAL_SCHEMA_SQL = """
create table if not exists shelfwise_connector_credentials (
    tenant_id text not null,
    system text not null,
    encrypted_payload text not null,
    updated_at timestamptz not null default now(),
    primary key (tenant_id, system)
);
"""


class PostgresConnectorCredentialStore:
    """Durable, RLS-scoped, encrypted-at-rest connector credential storage.

    The database never sees plaintext - `encrypted_payload` is the Fernet ciphertext of the
    credential field map, so even a database-level read (backup, replica, an operator with
    read access but not the encryption key) cannot recover a tenant's ERP credentials.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresConnectorCredentialStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert(
        self, *, tenant_id: str, system: SourceSystem, fields: dict[str, str]
    ) -> None:
        _validate_write(tenant_id, system, fields)
        token = encrypt_credential_fields(fields)
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_connector_credentials
                    (tenant_id, system, encrypted_payload, updated_at)
                values (%s, %s, %s, now())
                on conflict (tenant_id, system) do update
                set encrypted_payload = excluded.encrypted_payload, updated_at = now()
                """,
                (tenant_id, system.value, token),
            )
            conn.commit()

    def get(self, *, tenant_id: str, system: SourceSystem) -> dict[str, str] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                "select encrypted_payload from shelfwise_connector_credentials "
                "where tenant_id = %s and system = %s",
                (tenant_id, system.value),
            ).fetchone()
        return decrypt_credential_fields(str(row["encrypted_payload"])) if row else None

    def delete(self, *, tenant_id: str, system: SourceSystem) -> None:
        with self._connect(tenant_id) as conn:
            conn.execute(
                "delete from shelfwise_connector_credentials "
                "where tenant_id = %s and system = %s",
                (tenant_id, system.value),
            )
            conn.commit()

    def list_configured_systems(self, *, tenant_id: str) -> tuple[SourceSystem, ...]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                "select system from shelfwise_connector_credentials where tenant_id = %s",
                (tenant_id,),
            ).fetchall()
        return tuple(SourceSystem(str(row["system"])) for row in rows)

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_connector_credentials")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_CREDENTIAL_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_connector_credentials",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_connector_credential_store() -> (
    InMemoryConnectorCredentialStore | PostgresConnectorCredentialStore
):
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryConnectorCredentialStore()
    if backend == "postgres":
        return PostgresConnectorCredentialStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def resolve_connector_credentials(
    store: Any,
    *,
    tenant_id: str,
    system: SourceSystem,
    env_fallback: dict[str, str],
) -> dict[str, str]:
    """Prefer a tenant's stored credentials; fall back to the process-wide env vars.

    `env_fallback` is whatever the caller would have used before this module existed (the
    existing `SHELFWISE_CONNECTOR_*` env-var reads in `connector_poll_service.py`) - passed
    in rather than read here, so this function has no opinion about which env vars a given
    connector needs. A tenant with stored credentials always wins over the shared env-var
    default; a tenant with none configured gets the same single-tenant behavior this
    deployment already relies on today, unchanged.
    """
    stored = store.get(tenant_id=tenant_id, system=system)
    if stored is not None:
        return stored
    return env_fallback
