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
