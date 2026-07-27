from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from threading import Lock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from shelfwise_storage import auto_schema_enabled, connect


@dataclass(frozen=True, slots=True)
class EdgeDevice:
    """Tenant/store scope and volatile signing secret for one provisioned edge device."""

    device_id: str
    tenant_id: str
    store_id: str
    hmac_secret: bytes
    active: bool = True


class EdgeDeviceRegistrationError(RuntimeError):
    """Raised when a device secret cannot be encrypted for durable storage."""


def _encryption_key() -> bytes:
    """Derive a Fernet key from the same `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY` the connector
    credential store uses - one key powers every "sensitive secret encrypted at rest" need
    in this app, simpler for an operator to configure than one env var per subsystem. Not
    imported from `shelfwise_connectors.credentials` deliberately - that would make this
    package (edge intake) depend on the connectors package, an unwanted layering coupling
    for two otherwise-unrelated concerns that happen to share one operational secret.
    """
    raw = os.getenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise EdgeDeviceRegistrationError(
            "SHELFWISE_CREDENTIAL_ENCRYPTION_KEY must be set to provision or verify edge "
            "device secrets - refusing to store an unkeyed or hardcoded default"
        )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt_secret(secret: bytes) -> str:
    return Fernet(_encryption_key()).encrypt(secret).decode("ascii")


def _decrypt_secret(token: str) -> bytes:
    try:
        return Fernet(_encryption_key()).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise EdgeDeviceRegistrationError(
            "stored device secret could not be decrypted - wrong encryption key or "
            "corrupted data"
        ) from exc


class InMemoryEdgeDeviceRegistry:
    """Tenant-isolated in-memory registry for local development and tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._devices: dict[str, EdgeDevice] = {}
        self._batches: dict[tuple[str, str], str] = {}

    def register(self, device: EdgeDevice) -> None:
        """Register one new device without allowing an existing identity to be replaced."""
        if not device.device_id.strip() or not device.hmac_secret:
            raise ValueError("device_id and hmac_secret are required")
        with self._lock:
            if device.device_id in self._devices:
                raise ValueError("device_id is already registered")
            self._devices[device.device_id] = device

    def provision(self, device: EdgeDevice) -> dict[str, str]:
        """Provision a device and return non-secret identity metadata."""
        self.register(device)
        return {
            "device_id": device.device_id, "tenant_id": device.tenant_id,
            "store_id": device.store_id,
        }

    def list_devices(self, tenant_id: str, store_id: str | None = None) -> list[dict[str, object]]:
        """List device health metadata without returning signing secrets."""
        with self._lock:
            rows = [
                {
                    "device_id": item.device_id, "tenant_id": item.tenant_id,
                    "store_id": item.store_id, "active": item.active,
                }
                for item in self._devices.values()
                if item.tenant_id == tenant_id and (store_id is None or item.store_id == store_id)
            ]
        return sorted(rows, key=lambda row: str(row["device_id"]))

    def get_active(self, device_id: str) -> EdgeDevice | None:
        """Return an active device record or no record for failed-closed auth."""
        with self._lock:
            device = self._devices.get(device_id)
        return device if device and device.active else None

    def revoke(self, device_id: str, *, tenant_id: str, store_id: str) -> bool:
        """Atomically disable one tenant/store-scoped device without deleting its identity."""
        with self._lock:
            device = self._devices.get(device_id)
            if (
                device is None
                or not device.active
                or device.tenant_id != tenant_id
                or device.store_id != store_id
            ):
                return False
            self._devices[device_id] = EdgeDevice(
                device_id=device.device_id,
                tenant_id=device.tenant_id,
                store_id=device.store_id,
                hmac_secret=device.hmac_secret,
                active=False,
            )
            return True

    def claim_batch(self, tenant_id: str, batch_id: str) -> str:
        """Claim a batch for projection, preserving completed replay safety."""
        key = (tenant_id, batch_id)
        with self._lock:
            state = self._batches.get(key)
            if state is not None:
                return "in_progress" if state == "claimed" else state
            self._batches[key] = "claimed"
            return "claimed"

    def complete_batch(self, tenant_id: str, batch_id: str) -> bool:
        """Mark a successfully projected claim permanently replay-safe."""
        key = (tenant_id, batch_id)
        with self._lock:
            if self._batches.get(key) != "claimed":
                return False
            self._batches[key] = "completed"
            return True

    def release_batch(self, tenant_id: str, batch_id: str) -> bool:
        """Release a failed projection claim so the signed receipt can be retried."""
        key = (tenant_id, batch_id)
        with self._lock:
            if self._batches.get(key) != "claimed":
                return False
            del self._batches[key]
            return True

    def clear(self) -> None:
        """Clear disposable registry state between tests."""
        with self._lock:
            self._devices.clear()
            self._batches.clear()


_DEVICE_SCHEMA_SQL = """
create table if not exists shelfwise_edge_devices (
    device_id text primary key,
    tenant_id text not null,
    store_id text not null,
    encrypted_secret text not null,
    active boolean not null default true,
    created_at timestamptz not null default now()
);
"""


class PostgresEdgeDeviceRegistry:
    """Durable, encrypted-at-rest edge device registry.

    Closes the real gap the in-memory registry's own docstring flagged ("until durable
    device provisioning is selected") - a camera/sensor credential provisioned for a real
    client stack must survive a restart, or the integration silently breaks the first time
    the process redeploys. The database never sees the plaintext HMAC secret, matching the
    same encrypted-at-rest posture `shelfwise_connectors.credentials` uses.

    Deliberately NOT tenant-RLS-scoped, unlike every other tenant-owned table in this
    codebase - and this is a considered exception, not an oversight. `get_active(device_id)`
    is exactly how the edge-observation route resolves *which* tenant a request belongs to,
    from an opaque device_id alone, before any tenant is known - the very thing tenant RLS
    exists to gate on. Binding a tenant to look up the tenant would be circular, and with no
    tenant bound `current_setting('app.tenant_id')` is NULL, so RLS's own
    `tenant_id = current_setting(...)` policy would silently return zero rows for every
    lookup, permanently breaking the endpoint. Tenant scoping for `list_devices`/`revoke`
    is instead enforced at the application layer (explicit `where tenant_id = %s`
    predicates) - correct for this table's actual access pattern: `device_id` is a
    cryptographically random, unguessable token, and possessing it alone grants nothing
    without also knowing the paired HMAC secret to sign a valid batch.

    Batch replay-claim state (`claim_batch`/`complete_batch`/`release_batch`) intentionally
    stays in-memory per process even here - a lost claim on restart degrades to relying on
    `TwinService.accept`'s own idempotent per-observation dedup, not a correctness loss for
    already-registered devices, and durable batch-claim tracking is a separate concern from
    device identity durability.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresEdgeDeviceRegistry")
        self._database_url = database_url
        self._batches: dict[tuple[str, str], str] = {}
        self._batch_lock = Lock()
        if auto_schema_enabled():
            self._ensure_schema()

    def register(self, device: EdgeDevice) -> None:
        if not device.device_id.strip() or not device.hmac_secret:
            raise ValueError("device_id and hmac_secret are required")
        encrypted = _encrypt_secret(device.hmac_secret)
        with self._connect(device.tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_edge_devices
                    (device_id, tenant_id, store_id, encrypted_secret, active)
                values (%s, %s, %s, %s, %s)
                on conflict (device_id) do nothing
                returning device_id
                """,
                (device.device_id, device.tenant_id, device.store_id, encrypted, device.active),
            ).fetchone()
            if row is None:
                raise ValueError("device_id is already registered")
            conn.commit()

    def provision(self, device: EdgeDevice) -> dict[str, str]:
        self.register(device)
        return {
            "device_id": device.device_id, "tenant_id": device.tenant_id,
            "store_id": device.store_id,
        }

    def list_devices(self, tenant_id: str, store_id: str | None = None) -> list[dict[str, object]]:
        query = (
            "select device_id, tenant_id, store_id, active "
            "from shelfwise_edge_devices where tenant_id = %s"
        )
        params: list[Any] = [tenant_id]
        if store_id is not None:
            query += " and store_id = %s"
            params.append(store_id)
        query += " order by device_id"
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "device_id": row["device_id"], "tenant_id": row["tenant_id"],
                "store_id": row["store_id"], "active": row["active"],
            }
            for row in rows
        ]

    def get_active(self, device_id: str) -> EdgeDevice | None:
        # Device lookup by id alone (no known tenant yet - that's what this call resolves)
        # must bypass RLS's per-tenant scoping, which is why this is the one read path that
        # binds no tenant_id to the connection; every other method above is tenant-scoped.
        with self._connect(None) as conn:
            row = conn.execute(
                "select device_id, tenant_id, store_id, encrypted_secret, active "
                "from shelfwise_edge_devices where device_id = %s",
                (device_id,),
            ).fetchone()
        if row is None or not row["active"]:
            return None
        return EdgeDevice(
            device_id=row["device_id"], tenant_id=row["tenant_id"], store_id=row["store_id"],
            hmac_secret=_decrypt_secret(row["encrypted_secret"]), active=row["active"],
        )

    def revoke(self, device_id: str, *, tenant_id: str, store_id: str) -> bool:
        """Atomically revoke a device only within its tenant and store scope."""
        with self._connect(None) as conn:
            row = conn.execute(
                """
                update shelfwise_edge_devices
                set active = false
                where device_id = %s and tenant_id = %s and store_id = %s and active = true
                returning device_id
                """,
                (device_id, tenant_id, store_id),
            ).fetchone()
            if row is None:
                return False
            conn.commit()
        return True

    def claim_batch(self, tenant_id: str, batch_id: str) -> str:
        key = (tenant_id, batch_id)
        with self._batch_lock:
            state = self._batches.get(key)
            if state is not None:
                return "in_progress" if state == "claimed" else state
            self._batches[key] = "claimed"
            return "claimed"

    def complete_batch(self, tenant_id: str, batch_id: str) -> bool:
        key = (tenant_id, batch_id)
        with self._batch_lock:
            if self._batches.get(key) != "claimed":
                return False
            self._batches[key] = "completed"
            return True

    def release_batch(self, tenant_id: str, batch_id: str) -> bool:
        key = (tenant_id, batch_id)
        with self._batch_lock:
            if self._batches.get(key) != "claimed":
                return False
            del self._batches[key]
            return True

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_edge_devices")
            conn.commit()
        with self._batch_lock:
            self._batches.clear()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_DEVICE_SCHEMA_SQL)
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_edge_device_registry() -> InMemoryEdgeDeviceRegistry | PostgresEdgeDeviceRegistry:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryEdgeDeviceRegistry()
    if backend == "postgres":
        return PostgresEdgeDeviceRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")
