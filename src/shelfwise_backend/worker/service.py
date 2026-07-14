from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any

from .worker import CascadeWorker, WorkerResult


class WorkerLoopService:
    """Optional lifespan-managed queue consumer for async ingestion mode."""

    def __init__(
        self,
        worker: CascadeWorker,
        *,
        poll_s: float = 0.25,
        reclaim_interval_s: float = 30.0,
        reclaim_idle_ms: int = 30_000,
    ) -> None:
        self._worker = worker
        self._poll_s = max(0.01, poll_s)
        self._reclaim_interval_s = max(0.01, reclaim_interval_s)
        self._reclaim_idle_ms = max(0, reclaim_idle_ms)
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._reclaimed = 0
        self._last_reclaimed = 0
        self._reclaim_errors = 0
        self._last_reclaim_error: str | None = None
        self._last_status = "idle"
        self._last_error: str | None = None
        self._next_reclaim_at = 0.0

    async def start(self) -> None:
        if not worker_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._last_error = None
        self._next_reclaim_at = 0.0
        self._task = asyncio.create_task(self._run(), name="shelfwise-cascade-worker")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    def status(self) -> dict[str, Any]:
        task = self._task
        running = task is not None and not task.done()
        return {
            "enabled": worker_enabled(),
            "running": running,
            "processed": self._processed,
            "reclaimed": self._reclaimed,
            "last_reclaimed": self._last_reclaimed,
            "reclaim_errors": self._reclaim_errors,
            "last_reclaim_error": self._last_reclaim_error,
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        while True:
            try:
                await self._reclaim_if_due()
                result = await asyncio.to_thread(self._worker.process_one)
                self._record(result)
                if not result.processed:
                    await asyncio.sleep(self._poll_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_status = "crashed"
                self._last_error = str(exc)[:200]
                await asyncio.sleep(self._poll_s)

    async def _reclaim_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_reclaim_at:
            return
        self._next_reclaim_at = now + self._reclaim_interval_s
        try:
            reclaimed = await asyncio.to_thread(
                self._worker.reclaim_stale,
                min_idle_ms=self._reclaim_idle_ms,
            )
        except Exception as exc:
            message = str(exc)[:200]
            self._reclaim_errors += 1
            self._last_reclaim_error = message
            self._last_error = message
            return
        self._last_reclaimed = reclaimed
        self._reclaimed += reclaimed

    def _record(self, result: WorkerResult) -> None:
        self._last_status = result.status
        if result.processed:
            self._processed += 1
        if result.error:
            self._last_error = result.error[:200]


def worker_enabled() -> bool:
    return os.getenv("WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
