from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryInventoryPositionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._positions: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def upsert(self, position: dict[str, Any]) -> dict[str, Any]:
        payload = _position(position)
        key = _key(payload)
        with self._lock:
            self._positions[key] = payload
        return deepcopy(payload)

    def list(self, *, tenant_id: str, sku: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = [
                deepcopy(item)
                for item in self._positions.values()
                if item["tenant_id"] == tenant_id and (sku is None or item["sku"] == sku)
            ]
        return sorted(values, key=lambda item: (item["sku"], item["location_id"], item["bin_id"]))

    def clear(self) -> None:
        with self._lock:
            self._positions.clear()


class PostgresInventoryPositionStore:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresInventoryPositionStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert(self, position: dict[str, Any]) -> dict[str, Any]:
        payload = _position(position)
        with self._connect(payload["tenant_id"]) as conn:
            conn.execute(
                """
                insert into shelfwise_inventory_positions
                    (tenant_id, sku, location_type, location_id, bin_id, quantity, state,
                     source_reference, payload, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, sku, location_type, location_id, bin_id) do update
                set quantity = excluded.quantity, state = excluded.state,
                    source_reference = excluded.source_reference, payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["tenant_id"], payload["sku"], payload["location_type"],
                    payload["location_id"], payload["bin_id"], payload["quantity"],
                    payload["state"], payload["source_reference"], jsonb(payload),
                    payload["updated_at"],
                ),
            )
            conn.commit()
        return payload

    def list(self, *, tenant_id: str, sku: str | None = None) -> list[dict[str, Any]]:
        condition = "tenant_id = %s and sku = %s" if sku is not None else "tenant_id = %s"
        params = (tenant_id, sku) if sku is not None else (tenant_id,)
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                f"""
                select payload from shelfwise_inventory_positions
                where {condition}
                order by sku, location_id, bin_id
                """,
                params,
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_inventory_positions")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_POSITION_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_inventory_positions",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_inventory_position_store(
) -> InMemoryInventoryPositionStore | PostgresInventoryPositionStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryInventoryPositionStore()
    if backend == "postgres":
        return PostgresInventoryPositionStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _position(value: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(value)
    for field in ("tenant_id", "sku", "location_type", "location_id", "state", "source_reference"):
        payload[field] = str(payload.get(field) or "").strip()
        if not payload[field]:
            raise ValueError(f"inventory position {field} is required")
    payload["bin_id"] = str(payload.get("bin_id") or "unassigned").strip()
    payload["quantity"] = int(payload.get("quantity") or 0)
    if payload["quantity"] < 0:
        raise ValueError("inventory position quantity must be non-negative")
    payload["updated_at"] = str(payload.get("updated_at") or datetime.now(UTC).isoformat())
    return payload


def _key(value: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        value["tenant_id"], value["sku"], value["location_type"],
        value["location_id"], value["bin_id"],
    )


_POSITION_SCHEMA_SQL = """
create table if not exists shelfwise_inventory_positions (
    tenant_id text not null,
    sku text not null,
    location_type text not null,
    location_id text not null,
    bin_id text not null default 'unassigned',
    quantity integer not null check (quantity >= 0),
    state text not null,
    source_reference text not null,
    payload jsonb not null,
    updated_at timestamptz not null,
    primary key (tenant_id, sku, location_type, location_id, bin_id)
);
create index if not exists idx_shelfwise_inventory_positions_tenant_sku
on shelfwise_inventory_positions (tenant_id, sku, location_type);
"""
