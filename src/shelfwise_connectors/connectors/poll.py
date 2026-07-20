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

    async def delete(self, *, tenant_id: str, system: SourceSystem) -> None: ...

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

    async def delete(self, *, tenant_id: str, system: SourceSystem) -> None:
        with self._lock:
            self._cursors.pop((tenant_id, system.value), None)

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

    async def delete(self, *, tenant_id: str, system: SourceSystem) -> None:
        with self._connect(tenant_id) as conn:
            conn.execute(
                "delete from shelfwise_connector_cursors where tenant_id = %s and system = %s",
                (tenant_id, system.value),
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
    # Odoo returns a durable `write_date` watermark. ERP OData/SYSPRO connectors return
    # a continuation for one finite snapshot and must restart at page one after completing
    # that snapshot; retaining their final page token would poll only the final page forever.
    uses_incremental_cursor = False

    def __init__(self, cursors: InMemoryCursorStore, *, tenant_id: str) -> None:
        self._cursors = cursors
        self._tenant_id = tenant_id

    async def pull(self) -> AsyncIterator[InboundRecord]:
        cursor = await self._cursors.get(
            tenant_id=self._tenant_id,
            system=self.source_system,
        )
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
            # Persist only after callers have processed the page's records. If an intake
            # failure interrupts the generator, replaying the previous page is safe because
            # inbound-record deduplication prevents data loss or double application.
            await self._cursors.set(
                tenant_id=self._tenant_id,
                system=self.source_system,
                cursor=next_cursor,
            )
            cursor = next_cursor
        if not self.uses_incremental_cursor:
            # A completed pagination scan has no valid continuation to retain. Clearing the
            # page token makes the next scheduled poll read the full current snapshot.
            await self._cursors.delete(tenant_id=self._tenant_id, system=self.source_system)

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
