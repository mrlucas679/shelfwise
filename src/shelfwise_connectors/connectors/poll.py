from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from threading import Lock

from ..canonical import SourceSystem
from ..provenance import InboundRecord
from .base import SourceConnector


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
