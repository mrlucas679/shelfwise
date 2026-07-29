from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from typing import Any

from shelfwise_backend.event_bus import stale_consumer_idle_ms

from .worker import CascadeWorker, WorkerResult

_LOG = logging.getLogger("shelfwise.cascade_worker")


class WorkerLoopService:
    """Optional lifespan-managed queue consumer for async ingestion mode."""

    def __init__(
        self,
        worker: CascadeWorker,
        *,
        poll_s: float | None = None,
        reclaim_interval_s: float | None = None,
        reclaim_idle_ms: int | None = None,
    ) -> None:
        self._worker = worker
        self._poll_s = max(0.01, _float_env("SHELFWISE_WORKER_POLL_SECONDS", 0.25)
                           if poll_s is None else poll_s)
        # How often the sweep LOOKS for stale messages (a cheap read; frequent is fine).
        # How long a message must sit idle before it counts as stale is a different
        # question entirely and is budget-derived, not fixed: see
        # `stale_consumer_idle_ms()` - a threshold below the per-request work budget
        # steals live messages from healthy workers and double-runs them.
        self._reclaim_interval_s = max(
            0.01,
            _float_env("SHELFWISE_WORKER_RECLAIM_INTERVAL_SECONDS", 30.0)
            if reclaim_interval_s is None
            else reclaim_interval_s,
        )
        self._reclaim_idle_ms = (
            stale_consumer_idle_ms() if reclaim_idle_ms is None else max(0, reclaim_idle_ms)
        )
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._reclaimed = 0
        self._last_reclaimed = 0
        self._reclaim_errors = 0
        self._last_reclaim_error: str | None = None
        self._last_status = "idle"
        self._last_error: str | None = None
        self._next_reclaim_at = 0.0
        self._stop_requested = asyncio.Event()

    async def start(self) -> None:
        if not worker_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._last_error = None
        self._next_reclaim_at = 0.0
        self._stop_requested.clear()
        self._task = asyncio.create_task(self._run(), name="shelfwise-cascade-worker")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        # A cancellation here can interrupt `to_thread(process_one)` after it has
        # persisted a decision but before `_record` updates service diagnostics. Ask
        # the loop to stop instead so one in-flight, journaled unit completes cleanly.
        self._stop_requested.set()
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
        while not self._stop_requested.is_set():
            try:
                await self._reclaim_if_due()
                result = await asyncio.to_thread(self._worker.process_one)
                self._record(result)
                if not result.processed:
                    await _wait_for_stop(self._stop_requested, self._poll_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_status = "crashed"
                self._last_error = "worker_failed"
                _LOG.exception("cascade worker loop crashed")
                await _wait_for_stop(self._stop_requested, self._poll_s)

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
        except Exception:
            self._reclaim_errors += 1
            self._last_reclaim_error = "reclaim_failed"
            self._last_error = "reclaim_failed"
            _LOG.exception("cascade worker stale-message reclaim failed")
            return
        self._last_reclaimed = reclaimed
        self._reclaimed += reclaimed

    def _record(self, result: WorkerResult) -> None:
        self._last_status = result.status
        if result.processed:
            self._processed += 1
        if result.error:
            self._last_error = "event_processing_failed"


def worker_enabled() -> bool:
    return os.getenv("WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


async def _wait_for_stop(stop_requested: asyncio.Event, timeout: float) -> None:
    """Wait for either the next poll interval or a graceful shutdown request."""
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_requested.wait(), timeout=timeout)
