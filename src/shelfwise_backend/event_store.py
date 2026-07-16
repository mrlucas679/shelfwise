from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_contracts import Event
from shelfwise_runtime.provenance import DataDomain, normalize_domain
from shelfwise_storage import (
    auto_schema_enabled,
    connect,
    jsonb,
)
from shelfwise_storage import (
    now_iso as _now,
)
from shelfwise_storage import (
    validate_limit as _validate_limit,
)
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryEventStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._published: set[tuple[str, str, str]] = set()

    def record(self, event: Event) -> bool:
        payload = event.to_dict()
        payload.setdefault("received_at", _now())
        with self._lock:
            key = (event.tenant_id, event.data_domain.value, event.id)
            if key in self._events:
                return False
            self._events[key] = payload
            return True

    def is_published(
        self,
        event_id: str,
        *,
        tenant_id: str = "local",
        data_domain: DataDomain | str = DataDomain.OPERATIONAL_TWIN,
    ) -> bool:
        with self._lock:
            return (tenant_id, _domain(data_domain), event_id) in self._published

    def get(
        self,
        event_id: str,
        *,
        tenant_id: str,
        data_domain: DataDomain | str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._events.get((tenant_id, _domain(data_domain), event_id))
        return deepcopy(row) if row is not None else None

    def mark_published(
        self,
        event_id: str,
        *,
        tenant_id: str = "local",
        data_domain: DataDomain | str = DataDomain.OPERATIONAL_TWIN,
    ) -> None:
        with self._lock:
            self._published.add((tenant_id, _domain(data_domain), event_id))

    def list(
        self,
        *,
        limit: int = 200,
        tenant_id: str | None = None,
        data_domain: DataDomain | str | None = None,
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        resolved_domain = _domain(data_domain) if data_domain is not None else None
        with self._lock:
            rows = [
                row
                for (record_tenant, record_domain, _), row in self._events.items()
                if tenant_id is None or record_tenant == tenant_id
                if resolved_domain is None or record_domain == resolved_domain
            ][-limit:]
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
        with self._connect(event.tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_events
                    (id, tenant_id, data_domain, event_type, event_ts, payload, received_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict do nothing
                returning id
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.data_domain.value,
                    event.type.value,
                    event.ts.isoformat(),
                    jsonb(payload),
                    received_at,
                ),
            ).fetchone()
            conn.commit()
        return row is not None

    def is_published(
        self,
        event_id: str,
        *,
        tenant_id: str = "local",
        data_domain: DataDomain | str = DataDomain.OPERATIONAL_TWIN,
    ) -> bool:
        resolved_domain = _domain(data_domain)
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """select published from shelfwise_events
                   where tenant_id = %s and data_domain = %s and id = %s""",
                (tenant_id, resolved_domain, event_id),
            ).fetchone()
        return bool(row and row["published"])

    def get(
        self,
        event_id: str,
        *,
        tenant_id: str,
        data_domain: DataDomain | str,
    ) -> dict[str, Any] | None:
        resolved_domain = _domain(data_domain)
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """select payload from shelfwise_events
                   where tenant_id = %s and data_domain = %s and id = %s""",
                (tenant_id, resolved_domain, event_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def mark_published(
        self,
        event_id: str,
        *,
        tenant_id: str = "local",
        data_domain: DataDomain | str = DataDomain.OPERATIONAL_TWIN,
    ) -> None:
        resolved_domain = _domain(data_domain)
        with self._connect(tenant_id) as conn:
            conn.execute(
                """update shelfwise_events set published = true
                   where tenant_id = %s and data_domain = %s and id = %s""",
                (tenant_id, resolved_domain, event_id),
            )
            conn.commit()

    def list(
        self,
        *,
        limit: int = 200,
        tenant_id: str | None = None,
        data_domain: DataDomain | str | None = None,
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        resolved_domain = _domain(data_domain) if data_domain is not None else None
        with self._connect(tenant_id) as conn:
            if tenant_id and resolved_domain:
                rows = conn.execute(
                    """
                    select payload from shelfwise_events
                    where tenant_id = %s and data_domain = %s
                    order by received_at desc, id limit %s
                    """,
                    (tenant_id, resolved_domain, limit),
                ).fetchall()
            elif tenant_id:
                rows = conn.execute(
                    """
                    select payload
                    from shelfwise_events
                    where tenant_id = %s order by received_at desc, id
                    limit %s
                    """,
                    (tenant_id, limit),
                ).fetchall()
            elif resolved_domain:
                rows = conn.execute(
                    """
                    select payload from shelfwise_events
                    where data_domain = %s order by received_at desc, id limit %s
                    """,
                    (resolved_domain, limit),
                ).fetchall()
            else:
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
                    id text not null,
                    tenant_id text not null,
                    data_domain text not null default 'operational_twin',
                    event_type text not null,
                    event_ts timestamptz not null,
                    payload jsonb not null,
                    received_at timestamptz not null,
                    published boolean not null default false,
                    primary key (tenant_id, data_domain, id)
                )
                """
            )
            conn.execute(
                """alter table shelfwise_events
                   add column if not exists data_domain text"""
            )
            conn.execute(
                """update shelfwise_events
                   set data_domain = case
                       when payload->>'data_domain' in
                            ('operational_twin', 'world_simulation',
                             'training_fixture', 'twin_scenario')
                           then payload->>'data_domain'
                       when id like 'evt_demo_%'
                            or coalesce(payload->>'correlation_id', '') like 'world_%'
                           then 'world_simulation'
                       else 'operational_twin'
                   end
                   where data_domain is null"""
            )
            conn.execute(
                """alter table shelfwise_events
                   alter column data_domain set default 'operational_twin',
                   alter column data_domain set not null"""
            )
            conn.execute(
                "alter table shelfwise_events drop constraint if exists shelfwise_events_pkey"
            )
            conn.execute(
                "alter table shelfwise_events add primary key (tenant_id, data_domain, id)"
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
                on shelfwise_events (tenant_id, data_domain, received_at desc)
                """
            )
            apply_tenant_rls(conn, ("shelfwise_events",))
            conn.commit()

    def _connect(self, tenant_id: str | None = None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_event_store() -> InMemoryEventStore | PostgresEventStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryEventStore()
    if backend == "postgres":
        return PostgresEventStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _domain(value: DataDomain | str) -> str:
    return normalize_domain(value, default=DataDomain.OPERATIONAL_TWIN)
