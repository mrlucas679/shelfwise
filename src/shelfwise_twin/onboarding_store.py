"""Durable, replay-visible storage for the last-applied twin onboarding manifest per store.

`TwinService.onboard()` writes entities/relationships straight into the twin projection store,
which is not part of the durable `event_store` that `/twin/stores/{id}/bootstrap` replays. Without
this registry, a lost or rebuilt twin projection (the exact scenario `/bootstrap` exists to
recover from) would silently drop onboarded topology - seeded fixtures vanish and the store
entity reverts to its generic auto-created default. This registry gives the onboarding manifest
its own durable home so `bootstrap_events` can re-apply it before replaying events.
"""

from __future__ import annotations

import os
from threading import Lock
from typing import Any, Protocol

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls

from .models import TwinOnboardingManifest


class OnboardingManifestRegistry(Protocol):
    """Storage contract shared by the disposable local and durable Postgres runtimes."""

    def save(self, manifest: TwinOnboardingManifest) -> None: ...

    def get(self, tenant_id: str, store_id: str) -> TwinOnboardingManifest | None: ...

    def clear(self) -> None: ...


class InMemoryOnboardingManifestRegistry:
    """Process-local onboarding manifest registry with tenant/store isolation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._manifests: dict[tuple[str, str], TwinOnboardingManifest] = {}

    def save(self, manifest: TwinOnboardingManifest) -> None:
        with self._lock:
            self._manifests[(manifest.tenant_id, manifest.store_id)] = manifest

    def get(self, tenant_id: str, store_id: str) -> TwinOnboardingManifest | None:
        with self._lock:
            return self._manifests.get((tenant_id, store_id))

    def clear(self) -> None:
        with self._lock:
            self._manifests.clear()


class PostgresOnboardingManifestRegistry:
    """Durable onboarding manifest registry protected by tenant RLS."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresOnboardingManifestRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def save(self, manifest: TwinOnboardingManifest) -> None:
        with self._connect(manifest.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_twin_onboarding_manifests
                    (tenant_id, store_id, manifest)
                values (%s, %s, %s)
                on conflict (tenant_id, store_id) do update set
                    manifest = excluded.manifest
                """,
                (manifest.tenant_id, manifest.store_id, jsonb(manifest.model_dump(mode="json"))),
            )
            conn.commit()

    def get(self, tenant_id: str, store_id: str) -> TwinOnboardingManifest | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select manifest
                from shelfwise_twin_onboarding_manifests
                where tenant_id = %s and store_id = %s
                """,
                (tenant_id, store_id),
            ).fetchone()
        return TwinOnboardingManifest.model_validate(row["manifest"]) if row else None

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_twin_onboarding_manifests")
            conn.commit()

    def _ensure_schema(self) -> None:
        """Create the additive onboarding-manifest table before first use in local Postgres."""
        with self._connect(None) as conn:
            for statement in ONBOARDING_MANIFEST_SCHEMA_SQL:
                conn.execute(statement)
            apply_tenant_rls(conn, ONBOARDING_MANIFEST_TABLES)
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


ONBOARDING_MANIFEST_TABLES = ("shelfwise_twin_onboarding_manifests",)

ONBOARDING_MANIFEST_SCHEMA_SQL = (
    """
    create table if not exists shelfwise_twin_onboarding_manifests (
        tenant_id text not null, store_id text not null,
        manifest jsonb not null,
        primary key (tenant_id, store_id)
    )
    """,
)


def create_onboarding_manifest_registry() -> (
    InMemoryOnboardingManifestRegistry | PostgresOnboardingManifestRegistry
):
    """Create the registry using the same backend switch as other twin stores."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryOnboardingManifestRegistry()
    if backend == "postgres":
        return PostgresOnboardingManifestRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")
