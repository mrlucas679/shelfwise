from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryWorldSnapshotStore:
    """Process-local generated-world snapshot, keyed by tenant."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def save(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = _snapshot(snapshot)
        with self._lock:
            self._snapshots[payload["tenant_id"]] = payload
        return deepcopy(payload)

    def get(self, tenant_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(tenant_id)
        return deepcopy(snapshot) if snapshot is not None else None

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()


class PostgresWorldSnapshotStore:
    """Postgres-backed generated-world snapshot, one row per tenant."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresWorldSnapshotStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def save(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = _snapshot(snapshot)
        with self._connect(payload["tenant_id"]) as conn:
            conn.execute(
                """
                insert into shelfwise_world_snapshot
                    (tenant_id, seed, policy, generated_at, payload)
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id) do update
                set seed = excluded.seed,
                    policy = excluded.policy,
                    generated_at = excluded.generated_at,
                    payload = excluded.payload
                """,
                (
                    payload["tenant_id"],
                    payload["seed"],
                    payload["policy"],
                    payload["generated_at"],
                    jsonb(payload["payload"]),
                ),
            )
            conn.commit()
        return payload

    def get(self, tenant_id: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select tenant_id, seed, policy, generated_at, payload
                from shelfwise_world_snapshot
                where tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "tenant_id": row["tenant_id"],
            "seed": row["seed"],
            "policy": row["policy"],
            "generated_at": (
                row["generated_at"].isoformat()
                if hasattr(row["generated_at"], "isoformat")
                else str(row["generated_at"])
            ),
            "payload": row["payload"],
        }

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_world_snapshot")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_WORLD_SNAPSHOT_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_world_snapshot",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_world_snapshot_store() -> InMemoryWorldSnapshotStore | PostgresWorldSnapshotStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryWorldSnapshotStore()
    if backend == "postgres":
        return PostgresWorldSnapshotStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _snapshot(value: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(value.get("tenant_id") or "").strip()
    if not tenant_id:
        raise ValueError("world snapshot tenant_id is required")
    policy = str(value.get("policy") or "").strip()
    if not policy:
        raise ValueError("world snapshot policy is required")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("world snapshot payload must be a dict")
    return {
        "tenant_id": tenant_id,
        "seed": int(value.get("seed") or 0),
        "policy": policy,
        "generated_at": str(value.get("generated_at") or datetime.now(UTC).isoformat()),
        "payload": deepcopy(payload),
    }


_WORLD_SNAPSHOT_SCHEMA_SQL = """
create table if not exists shelfwise_world_snapshot (
    tenant_id text primary key,
    seed integer not null,
    policy text not null,
    generated_at timestamptz not null,
    payload jsonb not null
);
"""
