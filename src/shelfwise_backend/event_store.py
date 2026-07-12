from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_contracts import Event
from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryEventStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: dict[str, dict[str, Any]] = {}
        self._published: set[str] = set()

    def record(self, event: Event) -> bool:
        payload = event.to_dict()
        payload.setdefault("received_at", _now())
        with self._lock:
            if event.id in self._events:
                return False
            self._events[event.id] = payload
            return True

    def is_published(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._published

    def mark_published(self, event_id: str) -> None:
        with self._lock:
            self._published.add(event_id)

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            rows = list(self._events.values())[-limit:]
        return [deepcopy(row) for row in reversed(rows)]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._published.clear()


class PostgresEventStore:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresEventStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(self, event: Event) -> bool:
        payload = event.to_dict()
        received_at = _now()
        payload["received_at"] = received_at
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into shelfwise_events
                    (id, tenant_id, event_type, event_ts, payload, received_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (id) do nothing
                returning id
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.type.value,
                    event.ts.isoformat(),
                    jsonb(payload),
                    received_at,
                ),
            ).fetchone()
            conn.commit()
        return row is not None

    def is_published(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select published from shelfwise_events where id = %s",
                (event_id,),
            ).fetchone()
        return bool(row and row["published"])

    def mark_published(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "update shelfwise_events set published = true where id = %s",
                (event_id,),
            )
            conn.commit()

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                """
                select payload
                from shelfwise_events
                order by received_at desc, id
                limit %s
                """,
                (limit,),
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_events")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists shelfwise_events (
                    id text primary key,
                    tenant_id text not null,
                    event_type text not null,
                    event_ts timestamptz not null,
                    payload jsonb not null,
                    received_at timestamptz not null
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_events
                add column if not exists published boolean not null default false
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_events_tenant_received
                on shelfwise_events (tenant_id, received_at desc)
                """
            )
            apply_tenant_rls(conn, ("shelfwise_events",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def create_event_store() -> InMemoryEventStore | PostgresEventStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryEventStore()
    if backend == "postgres":
        return PostgresEventStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
