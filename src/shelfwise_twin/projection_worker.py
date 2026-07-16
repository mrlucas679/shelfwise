"""Recoverable event-bus consumer for asynchronous twin projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shelfwise_contracts import Event
from shelfwise_runtime.provenance import DataDomainBoundaryError

from .service import TwinService


class ProjectionBus(Protocol):
    """Minimal event-bus surface shared by memory and Redis Streams implementations."""

    def consume_one(self, stream: str | None = None, **kwargs: Any) -> dict[str, Any] | None: ...
    def ack(self, stream: str, message_id: str, **kwargs: Any) -> None: ...
    def nack(self, stream: str, message_id: str, **kwargs: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    """Describe one worker attempt without leaking source payloads."""

    status: str
    message_id: str | None = None
    observations_projected: int = 0
    dead_lettered: bool = False
    error: str | None = None


class TwinProjectionWorker:
    """Project canonical events with ack-after-success and Redis pending recovery."""

    def __init__(self, bus: ProjectionBus, service: TwinService, *, group: str = "twin") -> None:
        self.bus = bus
        self.service = service
        self.group = group

    def reclaim(self, *, consumer: str, min_idle_ms: int = 30_000) -> int:
        """Reassign abandoned Redis pending messages to the active consumer."""
        reclaim = getattr(self.bus, "reclaim_stale", None)
        if reclaim is None:
            return 0
        return int(reclaim(group=self.group, consumer=consumer, min_idle_ms=min_idle_ms))

    def run_once(self, *, consumer: str = "twin-worker") -> ProjectionReceipt:
        """Process one message; failures remain pending or move to the bus dead letter stream."""
        message = self.bus.consume_one(group=self.group, consumer=consumer)
        if not message:
            return ProjectionReceipt(status="empty")
        message_id = str(message.get("message_id", ""))
        stream = str(message.get("stream", ""))
        try:
            event = Event.parse_wire(message["event"])
            results = self.service.project_event(event)
        except DataDomainBoundaryError:
            self.bus.ack(stream, message_id, group=self.group)
            return ProjectionReceipt(
                status="skipped_non_operational",
                message_id=message_id,
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            dead_lettered = bool(
                self.bus.nack(stream, message_id, group=self.group)
            )
            return ProjectionReceipt(
                status="dead_lettered" if dead_lettered else "retry",
                message_id=message_id,
                dead_lettered=dead_lettered,
                error=type(exc).__name__,
            )
        self.bus.ack(stream, message_id, group=self.group)
        return ProjectionReceipt(
            status="projected", message_id=message_id,
            observations_projected=len(results),
        )

import asyncio  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
from contextlib import suppress  # noqa: E402


def twin_projection_worker_enabled() -> bool:
    return os.getenv("TWIN_PROJECTION_WORKER_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class TwinProjectionLoopService:
    """Optional lifespan-managed loop that drains the twin consumer group.

    Closes the plan's flagged gap: TwinProjectionWorker was built and unit-tested but
    never wired to run. It runs as a SUPPLEMENT to the inline projection every ingest
    already performs (projection is idempotent, so both running is safe): its job is
    recovery - replaying events whose inline projection was lost to a crash, and
    consuming on replicas that never saw the original request.

    Requires the Redis bus: the in-memory bus has no consumer groups, so enabling this
    service there would steal messages from the cascade worker instead of reading an
    independent group. When enabled against a memory bus it refuses to start and says
    so in status(), rather than silently corrupting the queue.
    """

    def __init__(
        self,
        worker: TwinProjectionWorker,
        *,
        poll_s: float = 0.25,
        reclaim_interval_s: float = 30.0,
        reclaim_idle_ms: int | None = None,
    ) -> None:
        self._worker = worker
        self._poll_s = max(0.01, poll_s)
        self._reclaim_interval_s = max(0.01, reclaim_interval_s)
        # The idle threshold before presuming a consumer dead is budget-derived by the
        # caller (see shelfwise_backend.event_bus.stale_consumer_idle_ms); when nothing
        # is supplied, reclaim is skipped entirely rather than guessing a number that
        # could sit inside a live worker's processing budget.
        self._reclaim_idle_ms = reclaim_idle_ms
        self._consumer = f"twin-{socket.gethostname()}-{os.getpid()}"
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._reclaimed = 0
        self._last_status = "idle"
        self._last_error: str | None = None
        self._refused_reason: str | None = None
        self._next_reclaim_at = 0.0

    async def start(self) -> None:
        if not twin_projection_worker_enabled():
            return
        if os.getenv("SHELFWISE_BUS_BACKEND", "memory").strip().lower() != "redis":
            self._refused_reason = (
                "twin projection worker requires SHELFWISE_BUS_BACKEND=redis - the "
                "in-memory bus has no consumer groups, so a second consumer would "
                "steal cascade messages instead of reading its own group"
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._last_error = None
        self._task = asyncio.create_task(self._run(), name="shelfwise-twin-projection")

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
        return {
            "enabled": twin_projection_worker_enabled(),
            "running": task is not None and not task.done(),
            "refused_reason": self._refused_reason,
            "consumer": self._consumer,
            "processed": self._processed,
            "reclaimed": self._reclaimed,
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        import time

        while True:
            try:
                if (
                    self._reclaim_idle_ms is not None
                    and time.monotonic() >= self._next_reclaim_at
                ):
                    self._next_reclaim_at = time.monotonic() + self._reclaim_interval_s
                    self._reclaimed += await asyncio.to_thread(
                        self._worker.reclaim,
                        consumer=self._consumer,
                        min_idle_ms=self._reclaim_idle_ms,
                    )
                receipt = await asyncio.to_thread(
                    self._worker.run_once, consumer=self._consumer
                )
                self._last_status = receipt.status
                if receipt.status == "empty":
                    await asyncio.sleep(self._poll_s)
                else:
                    self._processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_status = "crashed"
                self._last_error = str(exc)[:200]
                await asyncio.sleep(self._poll_s)
