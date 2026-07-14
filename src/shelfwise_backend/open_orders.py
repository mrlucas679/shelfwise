"""Canonical open-order state derived from shipment observations."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_contracts import Event, EventType
from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage import validate_limit as _validate_limit
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryOpenOrderStore:
    """Tenant-scoped open-order ledger for the memory runtime."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._orders: dict[tuple[str, str, str], dict[str, Any]] = {}

    def observe_event(self, event: Event) -> dict[str, Any] | None:
        """Upsert a shipment observation and return its canonical order state."""
        payload = _shipment_payload(event)
        if payload is None:
            return None
        key = (event.tenant_id, event.data_domain.value, payload["order_id"])
        with self._lock:
            existing = self._orders.get(key, {})
            record = _order_record(event, payload, existing)
            self._orders[key] = record
            return deepcopy(record)

    def list(
        self, tenant_id: str, *, data_domain: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return bounded order state for one tenant."""
        _validate_limit(limit)
        with self._lock:
            records = [
                deepcopy(record)
                for (record_tenant, record_domain, _), record in self._orders.items()
                if record_tenant == tenant_id
                and (data_domain is None or record_domain == data_domain)
            ]
        return sorted(records, key=lambda item: item["updated_at"], reverse=True)[:limit]

    def coverage(
        self, tenant_id: str, *, data_domain: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Aggregate remaining units and earliest ETA by SKU for suppression decisions."""
        coverage: dict[str, dict[str, Any]] = {}
        with self._lock:
            orders = [
                deepcopy(order)
                for (record_tenant, record_domain, _), order in self._orders.items()
                if record_tenant == tenant_id
                and (data_domain is None or record_domain == data_domain)
            ]
        for order in orders:
            if order["status"] != "open" or order["remaining_units"] <= 0:
                continue
            item = coverage.setdefault(
                order["sku"],
                {"remaining_units": 0, "etas": [], "order_ids": []},
            )
            item["remaining_units"] += order["remaining_units"]
            item["order_ids"].append(order["order_id"])
            if order.get("eta"):
                item["etas"].append(order["eta"])
        for item in coverage.values():
            item["eta"] = min(item["etas"]) if item["etas"] else None
            item.pop("etas", None)
        return coverage

    def clear(self) -> None:
        """Clear the disposable in-memory ledger."""
        with self._lock:
            self._orders.clear()


class PostgresOpenOrderStore:
    """Durable shipment/order ledger protected by tenant RLS."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresOpenOrderStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def observe_event(self, event: Event) -> dict[str, Any] | None:
        """Upsert a shipment observation by tenant and source order id."""
        payload = _shipment_payload(event)
        if payload is None:
            return None
        existing = self._get(event.tenant_id, event.data_domain.value, payload["order_id"])
        record = _order_record(event, payload, existing or {})
        with self._connect(event.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_open_orders
                    (tenant_id, data_domain, order_id, sku, supplier_id,
                     ordered_units, received_units, remaining_units, eta, status,
                     source_event_id, updated_at, payload)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, data_domain, order_id) do update set
                    sku = excluded.sku,
                    supplier_id = excluded.supplier_id,
                    ordered_units = excluded.ordered_units,
                    received_units = excluded.received_units,
                    remaining_units = excluded.remaining_units,
                    eta = excluded.eta,
                    status = excluded.status,
                    source_event_id = excluded.source_event_id,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    record["tenant_id"],
                    record["data_domain"],
                    record["order_id"],
                    record["sku"],
                    record["supplier_id"],
                    record["ordered_units"],
                    record["received_units"],
                    record["remaining_units"],
                    record["eta"],
                    record["status"],
                    record["source_event_id"],
                    record["updated_at"],
                    jsonb(record),
                ),
            )
            conn.commit()
        return record

    def list(
        self, tenant_id: str, *, data_domain: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return bounded order state for one tenant."""
        _validate_limit(limit)
        with self._connect(tenant_id) as conn:
            if data_domain is None:
                rows = conn.execute(
                    """select payload from shelfwise_open_orders
                    where tenant_id = %s order by updated_at desc limit %s""",
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """select payload from shelfwise_open_orders
                    where tenant_id = %s and data_domain = %s
                    order by updated_at desc limit %s""",
                    (tenant_id, data_domain, limit),
                ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def coverage(
        self, tenant_id: str, *, data_domain: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Aggregate remaining open units by SKU for candidate suppression."""
        with self._connect(tenant_id) as conn:
            domain_clause = "and data_domain = %s" if data_domain is not None else ""
            params = (tenant_id, data_domain) if data_domain is not None else (tenant_id,)
            rows = conn.execute(
                f"""
                select sku, sum(remaining_units) as remaining_units,
                       min(eta) as eta, array_agg(order_id) as order_ids
                from shelfwise_open_orders
                where tenant_id = %s and status = 'open' and remaining_units > 0
                {domain_clause}
                group by sku
                """,
                params,
            ).fetchall()
        return {
            str(row["sku"]): {
                "remaining_units": int(row["remaining_units"] or 0),
                "eta": row["eta"].isoformat() if row["eta"] is not None else None,
                "order_ids": list(row["order_ids"] or []),
            }
            for row in rows
        }

    def clear(self) -> None:
        """Clear orders visible to the active tenant."""
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_open_orders")
            conn.commit()

    def _get(self, tenant_id: str, data_domain: str, order_id: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """select payload from shelfwise_open_orders
                where tenant_id = %s and data_domain = %s and order_id = %s""",
                (tenant_id, data_domain, order_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_OPEN_ORDER_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_open_orders",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_open_order_store() -> InMemoryOpenOrderStore | PostgresOpenOrderStore:
    """Create the order ledger using the existing storage backend switch."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryOpenOrderStore()
    if backend == "postgres":
        return PostgresOpenOrderStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _shipment_payload(event: Event) -> dict[str, Any] | None:
    if event.type is not EventType.SHIPMENT:
        return None
    payload = event.payload
    try:
        ordered_units = int(payload.get("ordered_units") or payload.get("quantity") or 0)
        received_units = int(payload.get("received_units") or 0)
    except (TypeError, ValueError):
        return None
    sku = str(payload.get("sku") or "").strip()
    if not sku or ordered_units <= 0:
        return None
    eta_value = _parse_eta(payload.get("eta"))
    if payload.get("eta") and eta_value is None:
        return None
    return {
        "order_id": str(payload.get("order_id") or payload.get("po_id") or f"po:{event.id}"),
        "sku": sku,
        "supplier_id": str(payload.get("supplier_id") or payload.get("supplier") or "unknown"),
        "ordered_units": ordered_units,
        "received_units": max(received_units, 0),
        "eta": eta_value,
    }


def _order_record(
    event: Event,
    payload: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any]:
    event_ts = _event_timestamp(event)
    if existing and _is_stale_event(event_ts, existing.get("event_ts")):
        return dict(existing)
    ordered = payload["ordered_units"]
    received = payload["received_units"]
    if existing:
        received = max(received, int(existing.get("received_units") or 0))
    remaining = max(ordered - received, 0)
    return {
        "tenant_id": event.tenant_id,
        "data_domain": event.data_domain.value,
        **payload,
        "received_units": received,
        "remaining_units": remaining,
        "status": "open" if remaining > 0 else "fulfilled",
        "source_event_id": event.id,
        "event_ts": event_ts,
        "updated_at": event_ts,
    }


def _parse_eta(value: Any) -> str | None:
    """Normalize shipment ETAs so memory and Postgres enforce the same contract."""
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _event_timestamp(event: Event) -> str:
    timestamp = event.ts
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def _is_stale_event(incoming: str, existing: Any) -> bool:
    if not existing:
        return False
    try:
        current = datetime.fromisoformat(str(existing).replace("Z", "+00:00"))
    except ValueError:
        return False
    incoming_dt = datetime.fromisoformat(incoming)
    return incoming_dt < current


_OPEN_ORDER_SCHEMA_SQL = """
create table if not exists shelfwise_open_orders (
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    order_id text not null,
    sku text not null,
    supplier_id text not null,
    ordered_units integer not null,
    received_units integer not null,
    remaining_units integer not null,
    eta timestamptz,
    status text not null,
    source_event_id text not null,
    updated_at timestamptz not null,
    payload jsonb not null,
    primary key (tenant_id, data_domain, order_id)
);
alter table shelfwise_open_orders
add column if not exists data_domain text not null default 'operational_twin';
alter table shelfwise_open_orders
drop constraint if exists shelfwise_open_orders_pkey;
alter table shelfwise_open_orders
add primary key (tenant_id, data_domain, order_id);
drop index if exists idx_shelfwise_open_orders_tenant_sku_status;
create index if not exists idx_shelfwise_open_orders_tenant_sku_status
on shelfwise_open_orders (tenant_id, data_domain, sku, status, eta);
"""
