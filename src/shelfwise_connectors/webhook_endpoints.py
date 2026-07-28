"""Per-tenant, self-serve webhook endpoints for signature-authenticated source systems.

Closes the gap the connector-credentials work explicitly left open: Shopify, Square,
Lightspeed, and Yoco authenticate a *sender* with an HMAC signature rather than a
credential ShelfWise stores and replays, so they were excluded from the credential panel
and still needed an operator to wire the shared ingest API key into the retailer's webhook
configuration. A shop owner could not connect their own POS without a developer.

This registry gives each tenant its own opaque endpoint id and signing secret per system.
The owner provisions one in the product, pastes the returned URL and secret into their
retailer's webhook settings, and deliveries authenticate on the signature alone - no shared
ShelfWise API key ever leaves the operator's control, and revoking one tenant's endpoint
cannot affect another's.

Encrypted at rest with the same `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY` mechanism the
connector-credential and edge-device stores use, so an operator configures one secret for
every "sensitive value at rest" need rather than one per subsystem.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from threading import Lock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from shelfwise_storage import auto_schema_enabled, connect

from .canonical import SourceSystem

# Only systems whose transport is a signed webhook can have an endpoint provisioned. Poll
# systems (Odoo/SAP/SYSPRO/Dynamics) authenticate outbound with stored credentials instead,
# and CSV is a file upload - offering an endpoint for either would imply a delivery path
# that does not exist.
WEBHOOK_SYSTEMS = frozenset(
    {
        SourceSystem.SHOPIFY,
        SourceSystem.SQUARE,
        SourceSystem.LIGHTSPEED,
        SourceSystem.YOCO,
    }
)


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    """Tenant scope and signing secret for one provisioned source-system webhook."""

    endpoint_id: str
    tenant_id: str
    system: SourceSystem
    signing_secret: str
    active: bool = True


class WebhookEndpointError(RuntimeError):
    """Raised when an endpoint secret cannot be encrypted or decrypted."""


class WebhookSystemNotSupported(ValueError):
    """Raised for a system that does not authenticate by webhook signature."""


def _encryption_key() -> bytes:
    raw = os.getenv("SHELFWISE_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise WebhookEndpointError(
            "SHELFWISE_CREDENTIAL_ENCRYPTION_KEY must be set to provision or verify webhook "
            "endpoint secrets - refusing to store an unkeyed or hardcoded default"
        )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt(secret: str) -> str:
    return Fernet(_encryption_key()).encrypt(secret.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    try:
        return Fernet(_encryption_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise WebhookEndpointError(
            "stored webhook secret could not be decrypted - wrong encryption key or "
            "corrupted data"
        ) from exc


def resolve_webhook_system(system: str) -> SourceSystem:
    """Return the source system for a webhook-capable name, rejecting every other system."""
    try:
        resolved = SourceSystem(system.strip().lower())
    except ValueError as exc:
        raise WebhookSystemNotSupported("Unknown connector system") from exc
    if resolved not in WEBHOOK_SYSTEMS:
        raise WebhookSystemNotSupported(
            f"{resolved.value} does not authenticate by webhook signature"
        )
    return resolved


def new_endpoint_credentials() -> tuple[str, str]:
    """Mint an unguessable endpoint id and signing secret for a new webhook endpoint."""
    return f"whep_{secrets.token_urlsafe(24)}", secrets.token_urlsafe(32)


class InMemoryWebhookEndpointRegistry:
    """Tenant-isolated in-memory registry for local development and tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._endpoints: dict[str, WebhookEndpoint] = {}

    def register(self, endpoint: WebhookEndpoint) -> None:
        if not endpoint.endpoint_id.strip() or not endpoint.signing_secret:
            raise ValueError("endpoint_id and signing_secret are required")
        with self._lock:
            if endpoint.endpoint_id in self._endpoints:
                raise ValueError("endpoint_id is already registered")
            self._endpoints[endpoint.endpoint_id] = endpoint

    def list_endpoints(self, tenant_id: str) -> list[dict[str, object]]:
        """List endpoint metadata for one tenant, never returning a signing secret."""
        with self._lock:
            rows = [
                {
                    "endpoint_id": item.endpoint_id,
                    "system": item.system.value,
                    "active": item.active,
                }
                for item in self._endpoints.values()
                if item.tenant_id == tenant_id
            ]
        return sorted(rows, key=lambda row: str(row["endpoint_id"]))

    def get_active(self, endpoint_id: str) -> WebhookEndpoint | None:
        with self._lock:
            endpoint = self._endpoints.get(endpoint_id)
        return endpoint if endpoint and endpoint.active else None

    def revoke(self, endpoint_id: str, *, tenant_id: str) -> bool:
        """Atomically disable one tenant-scoped endpoint without deleting its identity."""
        with self._lock:
            endpoint = self._endpoints.get(endpoint_id)
            if endpoint is None or not endpoint.active or endpoint.tenant_id != tenant_id:
                return False
            self._endpoints[endpoint_id] = WebhookEndpoint(
                endpoint_id=endpoint.endpoint_id,
                tenant_id=endpoint.tenant_id,
                system=endpoint.system,
                signing_secret=endpoint.signing_secret,
                active=False,
            )
            return True

    def clear(self) -> None:
        with self._lock:
            self._endpoints.clear()


_ENDPOINT_SCHEMA_SQL = """
create table if not exists shelfwise_webhook_endpoints (
    endpoint_id text primary key,
    tenant_id text not null,
    system text not null,
    encrypted_secret text not null,
    active boolean not null default true,
    created_at timestamptz not null default now()
);
"""


class PostgresWebhookEndpointRegistry:
    """Durable, encrypted-at-rest per-tenant webhook endpoint registry.

    Deliberately NOT tenant-RLS-scoped, for the same reason `PostgresEdgeDeviceRegistry`
    is not: `get_active(endpoint_id)` is how an unauthenticated delivery resolves *which*
    tenant it belongs to, before any tenant is known. Binding a tenant in order to look up
    that tenant is circular, and with no tenant bound `current_setting('app.tenant_id')` is
    NULL, so an RLS policy would silently match zero rows and permanently break every
    webhook. Tenant scoping for `list_endpoints`/`revoke` is enforced at the application
    layer with explicit predicates instead, which is correct for this access pattern: the
    endpoint id is a cryptographically random token, and possessing it grants nothing
    without also knowing the paired signing secret.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresWebhookEndpointRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def register(self, endpoint: WebhookEndpoint) -> None:
        if not endpoint.endpoint_id.strip() or not endpoint.signing_secret:
            raise ValueError("endpoint_id and signing_secret are required")
        encrypted = _encrypt(endpoint.signing_secret)
        with self._connect(endpoint.tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_webhook_endpoints
                    (endpoint_id, tenant_id, system, encrypted_secret, active)
                values (%s, %s, %s, %s, %s)
                on conflict (endpoint_id) do nothing
                returning endpoint_id
                """,
                (
                    endpoint.endpoint_id,
                    endpoint.tenant_id,
                    endpoint.system.value,
                    encrypted,
                    endpoint.active,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("endpoint_id is already registered")
            conn.commit()

    def list_endpoints(self, tenant_id: str) -> list[dict[str, object]]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                "select endpoint_id, system, active from shelfwise_webhook_endpoints "
                "where tenant_id = %s order by endpoint_id",
                (tenant_id,),
            ).fetchall()
        return [
            {
                "endpoint_id": row["endpoint_id"],
                "system": row["system"],
                "active": row["active"],
            }
            for row in rows
        ]

    def get_active(self, endpoint_id: str) -> WebhookEndpoint | None:
        # Endpoint lookup by id alone binds no tenant - this call is what resolves the
        # tenant. See the class docstring for why RLS is intentionally not applied here.
        with self._connect(None) as conn:
            row = conn.execute(
                "select endpoint_id, tenant_id, system, encrypted_secret, active "
                "from shelfwise_webhook_endpoints where endpoint_id = %s",
                (endpoint_id,),
            ).fetchone()
        if row is None or not row["active"]:
            return None
        return WebhookEndpoint(
            endpoint_id=row["endpoint_id"],
            tenant_id=row["tenant_id"],
            system=SourceSystem(row["system"]),
            signing_secret=_decrypt(row["encrypted_secret"]),
            active=row["active"],
        )

    def revoke(self, endpoint_id: str, *, tenant_id: str) -> bool:
        with self._connect(None) as conn:
            row = conn.execute(
                """
                update shelfwise_webhook_endpoints
                set active = false
                where endpoint_id = %s and tenant_id = %s and active = true
                returning endpoint_id
                """,
                (endpoint_id, tenant_id),
            ).fetchone()
            if row is None:
                return False
            conn.commit()
        return True

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_webhook_endpoints")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_ENDPOINT_SCHEMA_SQL)
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_webhook_endpoint_registry() -> (
    InMemoryWebhookEndpointRegistry | PostgresWebhookEndpointRegistry
):
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryWebhookEndpointRegistry()
    if backend == "postgres":
        return PostgresWebhookEndpointRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")
