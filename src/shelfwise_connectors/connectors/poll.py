from __future__ import annotations

import os
from abc import abstractmethod
from collections.abc import AsyncIterator
from threading import Lock
from typing import Any, Protocol

from shelfwise_storage import auto_schema_enabled, connect
from shelfwise_storage.rls import apply_tenant_rls

from ..canonical import SourceSystem
from ..provenance import InboundRecord
from .base import SourceConnector


class CursorStore(Protocol):
    """Per-(tenant, system) pagination cursor, shared by the memory and Postgres backends."""

    async def get(self, *, tenant_id: str, system: SourceSystem) -> str | None: ...

    async def set(self, *, tenant_id: str, system: SourceSystem, cursor: str) -> None: ...

    def clear(self) -> None: ...


class InMemoryCursorStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cursors: dict[tuple[str, str], str] = {}

    async def get(self, *, tenant_id: str, system: SourceSystem) -> str | None:
        with self._lock:
            return self._cursors.get((tenant_id, system.value))

    async def set(self, *, tenant_id: str, system: SourceSystem, cursor: str) -> None:
        with self._lock:
            self._cursors[(tenant_id, system.value)] = cursor

    def clear(self) -> None:
        with self._lock:
            self._cursors.clear()


class PostgresCursorStore:
    """Durable poll cursor so a process restart resumes instead of re-polling from scratch.

    An in-memory-only cursor is a real gap for a poll connector specifically: every restart
    would silently re-fetch a system's entire history on the next poll rather than resuming
    - `pull()`'s own dedup absorbs the duplicates, but at the cost of re-fetching everything
    every time the process restarts, which does not scale to a real ERP catalogue.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresCursorStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    async def get(self, *, tenant_id: str, system: SourceSystem) -> str | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                "select cursor from shelfwise_connector_cursors "
                "where tenant_id = %s and system = %s",
                (tenant_id, system.value),
            ).fetchone()
        return str(row["cursor"]) if row else None

    async def set(self, *, tenant_id: str, system: SourceSystem, cursor: str) -> None:
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_connector_cursors (tenant_id, system, cursor)
                values (%s, %s, %s)
                on conflict (tenant_id, system) do update set cursor = excluded.cursor
                """,
                (tenant_id, system.value, cursor),
            )
            conn.commit()

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_connector_cursors")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_CURSOR_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_connector_cursors",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


_CURSOR_SCHEMA_SQL = """
create table if not exists shelfwise_connector_cursors (
    tenant_id text not null,
    system text not null,
    cursor text not null,
    primary key (tenant_id, system)
);
"""


def create_cursor_store() -> InMemoryCursorStore | PostgresCursorStore:
    """Create the poll-cursor store using the existing storage backend switch."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryCursorStore()
    if backend == "postgres":
        return PostgresCursorStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


_MAX_POLL_PAGES = 10_000


class PollingConnector(SourceConnector):
    source_system: SourceSystem

    def __init__(self, cursors: InMemoryCursorStore, *, tenant_id: str) -> None:
        self._cursors = cursors
        self._tenant_id = tenant_id

    async def pull(self) -> AsyncIterator[InboundRecord]:
        cursor = await self._cursors.get(
            tenant_id=self._tenant_id,
            system=self.source_system,
        )
        latest_cursor = cursor
        seen: set[tuple[str, str, str, str]] = set()
        # Bounded by _MAX_POLL_PAGES (NASA Power-of-Ten: every loop has a known upper
        # bound) and breaks early on a non-advancing cursor, so a misbehaving or hostile
        # source that keeps returning the same or an endless run of cursors can't spin
        # the event loop forever.
        for _ in range(_MAX_POLL_PAGES):
            page, next_cursor = await self.fetch_page(cursor)
            for record in page:
                key = _dedupe_key(record)
                if key in seen:
                    continue
                seen.add(key)
                yield record
            if next_cursor is None or next_cursor == cursor:
                break
            latest_cursor = next_cursor
            cursor = next_cursor
        if latest_cursor is not None:
            await self._cursors.set(
                tenant_id=self._tenant_id,
                system=self.source_system,
                cursor=latest_cursor,
            )

    @abstractmethod
    async def fetch_page(
        self,
        cursor: str | None,
    ) -> tuple[list[InboundRecord], str | None]:
        raise NotImplementedError


def _dedupe_key(record: InboundRecord) -> tuple[str, str, str, str]:
    return (
        record.source_system.value,
        record.source_object_type,
        record.source_object_id,
        record.payload_hash,
    )
