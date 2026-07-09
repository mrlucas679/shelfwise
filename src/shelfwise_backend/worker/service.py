from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any

from .worker import CascadeWorker, WorkerResult


class WorkerLoopService:
    """Optional lifespan-managed queue consumer for async ingestion mode."""

    def __init__(self, worker: CascadeWorker, *, poll_s: float = 0.25) -> None:
        self._worker = worker
        self._poll_s = max(0.01, poll_s)
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._last_status = "idle"
        self._last_error: str | None = None

    async def start(self) -> None:
        if not worker_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._last_error = None
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
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        while True:
            try:
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

    def _record(self, result: WorkerResult) -> None:
        self._last_status = result.status
        if result.processed:
            self._processed += 1
        if result.error:
            self._last_error = result.error[:200]


def worker_enabled() -> bool:
    return os.getenv("WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
