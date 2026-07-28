from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryInventoryPositionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._positions: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._projection_receipts: dict[tuple[str, str], dict[str, Any]] = {}

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

    def project_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Idempotently apply a normalized stock update or sale to the position ledger."""
        projection = _projection_input(event)
        if projection is None:
            return {"status": "ignored", "event_id": str(event.get("id") or "")}
        receipt_key = (projection["tenant_id"], projection["event_id"])
        with self._lock:
            existing = self._projection_receipts.get(receipt_key)
            if existing is not None:
                return {**deepcopy(existing), "status": "duplicate"}
            receipt = _apply_projection(self._positions, projection)
            self._projection_receipts[receipt_key] = receipt
            return deepcopy(receipt)

    def projection_receipt(self, tenant_id: str, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._projection_receipts.get((tenant_id, event_id))
            return deepcopy(receipt) if receipt else None

    def clear(self) -> None:
        with self._lock:
            self._positions.clear()
            self._projection_receipts.clear()


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
            self._upsert(conn, payload)
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

    def project_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Apply one event and its replay receipt in the same tenant-scoped transaction."""
        projection = _projection_input(event)
        if projection is None:
            return {"status": "ignored", "event_id": str(event.get("id") or "")}
        tenant_id = projection["tenant_id"]
        event_id = projection["event_id"]
        with self._connect(tenant_id) as conn:
            conn.execute(
                "select pg_advisory_xact_lock(hashtext(%s))",
                (f"inventory:{tenant_id}:{event_id}",),
            )
            existing = conn.execute(
                "select payload from shelfwise_inventory_projection_receipts "
                "where tenant_id = %s and event_id = %s",
                (tenant_id, event_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {**deepcopy(existing["payload"]), "status": "duplicate"}
            rows = conn.execute(
                "select payload from shelfwise_inventory_positions "
                "where tenant_id = %s and sku = %s and location_id = %s "
                "order by bin_id for update",
                (tenant_id, projection["sku"], projection["location_id"]),
            ).fetchall()
            positions = {_key(row["payload"]): deepcopy(row["payload"]) for row in rows}
            receipt = _apply_projection(positions, projection)
            for position in positions.values():
                self._upsert(conn, position)
            conn.execute(
                "insert into shelfwise_inventory_projection_receipts "
                "(tenant_id, event_id, payload, created_at) values (%s, %s, %s, %s)",
                (tenant_id, event_id, jsonb(receipt), receipt["created_at"]),
            )
            conn.commit()
        return receipt

    def projection_receipt(self, tenant_id: str, event_id: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                "select payload from shelfwise_inventory_projection_receipts "
                "where tenant_id = %s and event_id = %s",
                (tenant_id, event_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_inventory_projection_receipts")
            conn.execute("delete from shelfwise_inventory_positions")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_POSITION_SCHEMA_SQL)
            apply_tenant_rls(
                conn,
                (
                    "shelfwise_inventory_positions",
                    "shelfwise_inventory_projection_receipts",
                ),
            )
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)

    @staticmethod
    def _upsert(conn: Any, position: dict[str, Any]) -> None:
        payload = _position(position)
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
                payload["tenant_id"],
                payload["sku"],
                payload["location_type"],
                payload["location_id"],
                payload["bin_id"],
                payload["quantity"],
                payload["state"],
                payload["source_reference"],
                jsonb(payload),
                payload["updated_at"],
            ),
        )


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


def _projection_input(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "").strip().lower()
    data_domain = str(event.get("data_domain") or "operational_twin").strip()
    if data_domain != "operational_twin" or event_type not in {"stock_update", "sale"}:
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    tenant_id = str(event.get("tenant_id") or "").strip()
    event_id = str(event.get("id") or "").strip()
    sku = str(payload.get("sku") or "").strip()
    location_id = str(
        payload.get("location_id") or payload.get("location") or ""
    ).strip()
    issue = None
    if not all((tenant_id, event_id, sku, location_id)):
        issue = "missing_projection_fields"
    quantity = _integral_quantity(
        payload.get("on_hand", payload.get("quantity")),
    )
    if quantity is None:
        issue = "unsupported_quantity"
    elif event_type == "sale" and quantity <= 0:
        issue = "non_positive_sale_quantity"
    elif event_type == "stock_update" and quantity < 0:
        issue = "negative_stock_quantity"
    return {
        "tenant_id": tenant_id,
        "event_id": event_id,
        "event_type": event_type,
        "sku": sku,
        "location_id": location_id,
        "location_type": str(payload.get("location_type") or "store").strip(),
        "bin_id": str(payload.get("bin_id") or "unassigned").strip(),
        "quantity": quantity,
        "stock_state": str(payload.get("stock_state") or "available").strip(),
        "issue": issue,
    }


def _apply_projection(
    positions: dict[tuple[str, str, str, str, str], dict[str, Any]],
    projection: dict[str, Any],
) -> dict[str, Any]:
    if projection["issue"]:
        return _projection_receipt(projection, status=str(projection["issue"]))
    matching = sorted(
        (
            key
            for key, position in positions.items()
            if position["tenant_id"] == projection["tenant_id"]
            and position["sku"] == projection["sku"]
            and position["location_id"] == projection["location_id"]
        ),
        key=lambda key: key[-1],
    )
    before = sum(int(positions[key]["quantity"]) for key in matching)
    if projection["event_type"] == "stock_update":
        return _apply_stock_projection(positions, projection, before=before)
    return _apply_sale_projection(positions, projection, matching=matching, before=before)


def _apply_stock_projection(
    positions: dict[tuple[str, str, str, str, str], dict[str, Any]],
    projection: dict[str, Any],
    *,
    before: int,
) -> dict[str, Any]:
    position = _position(
        {
            "tenant_id": projection["tenant_id"],
            "sku": projection["sku"],
            "location_type": projection["location_type"],
            "location_id": projection["location_id"],
            "bin_id": projection["bin_id"],
            "quantity": projection["quantity"],
            "state": projection["stock_state"],
            "source_reference": projection["event_id"],
        }
    )
    position_key = _key(position)
    previous_quantity = int(positions.get(position_key, {}).get("quantity", 0))
    positions[position_key] = position
    return _projection_receipt(
        projection,
        status="stock_position_replaced",
        before_quantity=before,
        after_quantity=before - previous_quantity + int(projection["quantity"]),
        positions_updated=1,
    )


def _apply_sale_projection(
    positions: dict[tuple[str, str, str, str, str], dict[str, Any]],
    projection: dict[str, Any],
    *,
    matching: list[tuple[str, str, str, str, str]],
    before: int,
) -> dict[str, Any]:
    if not matching:
        return _projection_receipt(
            projection,
            status="missing_inventory_baseline",
            before_quantity=0,
            after_quantity=0,
            unfulfilled_quantity=int(projection["quantity"]),
        )
    remaining = int(projection["quantity"])
    updated_count = 0
    for key in matching:
        if remaining <= 0:
            break
        current = positions[key]
        deduction = min(int(current["quantity"]), remaining)
        if deduction <= 0:
            continue
        after = int(current["quantity"]) - deduction
        positions[key] = _position(
            {
                **current,
                "quantity": after,
                "state": "depleted" if after == 0 else current["state"],
                "source_reference": projection["event_id"],
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        remaining -= deduction
        updated_count += 1
    return _projection_receipt(
        projection,
        status="sale_applied" if remaining == 0 else "sale_partially_applied",
        before_quantity=before,
        after_quantity=before - (int(projection["quantity"]) - remaining),
        unfulfilled_quantity=remaining,
        positions_updated=updated_count,
    )


def _projection_receipt(
    projection: dict[str, Any],
    *,
    status: str,
    before_quantity: int | None = None,
    after_quantity: int | None = None,
    unfulfilled_quantity: int = 0,
    positions_updated: int = 0,
) -> dict[str, Any]:
    return {
        "tenant_id": projection["tenant_id"],
        "event_id": projection["event_id"],
        "event_type": projection["event_type"],
        "sku": projection["sku"],
        "location_id": projection["location_id"],
        "status": status,
        "requested_quantity": projection["quantity"],
        "before_quantity": before_quantity,
        "after_quantity": after_quantity,
        "unfulfilled_quantity": unfulfilled_quantity,
        "positions_updated": positions_updated,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _integral_quantity(value: object) -> int | None:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not quantity.is_finite() or quantity != quantity.to_integral_value():
        return None
    return int(quantity)


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
create table if not exists shelfwise_inventory_projection_receipts (
    tenant_id text not null,
    event_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, event_id)
);
"""
