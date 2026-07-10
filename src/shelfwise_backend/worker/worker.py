from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shelfwise_backend.cascade import (
    run_catalog_price_check,
    run_cold_chain_cascade,
    run_expiry_risk_check,
    run_golden_cascade,
    run_procurement_cascade,
    run_sales_cascade,
)
from shelfwise_contracts import Event, EventType

from .journal import InMemoryJournal, PostgresJournal, journaled

CascadeHandler = Callable[[Event], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    processed: bool
    status: str
    message_id: str | None = None
    run_id: str | None = None
    cascade: dict[str, Any] | None = None
    error: str | None = None
    dead_lettered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "status": self.status,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "cascade": self.cascade,
            "error": self.error,
            "dead_lettered": self.dead_lettered,
        }


class CascadeWorker:
    """Single-event worker for the current synchronous backend.

    Consume one event, journal the cascade step, journal decision persistence, then ack.
    On failure the message is never acked directly: it is nacked, which requeues it for
    another attempt or moves it to the bus's dead-letter queue once its retry budget is
    exhausted (see `EventBus.nack`) - a failed cascade never silently disappears. The
    journal run is keyed on the event's own id (not its correlation_id, which may be shared
    across unrelated events) so two different events never collide on the same journaled
    step. A background loop can call this repeatedly when the app is ready for worker mode;
    tests can drive it deterministically one message at a time.
    """

    def __init__(
        self,
        *,
        bus: Any,
        journal: InMemoryJournal | PostgresJournal,
        decision_store: Any,
        handler: CascadeHandler | None = None,
        group: str = "cascade",
        consumer: str = "worker-1",
    ) -> None:
        self._bus = bus
        self._journal = journal
        self._decision_store = decision_store
        self._handler = handler or default_cascade_handler
        self._group = group
        self._consumer = consumer

    def process_one(self, stream: str | None = None) -> WorkerResult:
        message = self._consume(stream)
        if message is None:
            return WorkerResult(processed=False, status="idle")

        message_id = str(message["message_id"])
        message_stream = str(message["stream"])
        try:
            event = Event.parse_wire(message["event"])
            run_id = event.id
            self._journal.start_run(run_id, tenant_id=event.tenant_id)
            cascade = journaled(
                self._journal,
                run_id,
                "cascade",
                lambda: self._handler(event),
            )
            persisted = self._persist_decision(run_id, cascade)
            if persisted is not None:
                cascade = {**cascade, "decision": persisted}
            self._journal.finish_run(run_id, status="done")
            self._ack(message_stream, message_id)
            return WorkerResult(
                processed=True,
                status="done",
                message_id=message_id,
                run_id=run_id,
                cascade=cascade,
            )
        except Exception as exc:
            run_id = _run_id_from_message(message)
            if run_id:
                self._journal.finish_run(run_id, status="failed")
            dead_lettered = self._nack(message_stream, message_id)
            return WorkerResult(
                processed=True,
                status="failed",
                message_id=message_id,
                run_id=run_id,
                error=str(exc),
                dead_lettered=dead_lettered,
            )

    def _persist_decision(self, run_id: str, cascade: dict[str, Any]) -> dict[str, Any] | None:
        decision = cascade.get("decision")
        if not isinstance(decision, dict):
            return None
        result = journaled(
            self._journal,
            run_id,
            f"decision_persist:{decision.get('id', 'unknown')}",
            lambda: {"decision": self._decision_store.upsert(decision)},
        )
        persisted = result.get("decision")
        return persisted if isinstance(persisted, dict) else None

    def _consume(self, stream: str | None) -> dict[str, Any] | None:
        try:
            return self._bus.consume_one(stream, group=self._group, consumer=self._consumer)
        except TypeError:
            return self._bus.consume_one(stream)

    def _ack(self, stream: str, message_id: str) -> None:
        try:
            self._bus.ack(stream, message_id, group=self._group)
        except TypeError:
            self._bus.ack(stream, message_id)

    def _nack(self, stream: str, message_id: str) -> bool:
        """Requeue or dead-letter a failed message. Returns True if dead-lettered."""
        try:
            return bool(self._bus.nack(stream, message_id, group=self._group))
        except TypeError:
            return bool(self._bus.nack(stream, message_id))


def default_cascade_handler(event: Event) -> dict[str, Any]:
    sku = str(event.payload.get("sku", ""))
    if event.type is EventType.SCAN and sku == "4011":
        return _attach_event_causality(run_golden_cascade(), event)
    supplier = str(event.payload.get("supplier", "")).lower()
    if event.type is EventType.SUPPLIER_UPDATE and supplier == "dairyco":
        return _attach_event_causality(run_procurement_cascade(), event)
    if event.type is EventType.SALE and sku == "4011":
        return _attach_event_causality(run_sales_cascade(event), event)
    if event.type is EventType.SALE:
        result = run_catalog_price_check(event)
        if result is not None:
            return _attach_event_causality(result, event)
    if event.type is EventType.EXPIRY_ENTRY:
        result = run_expiry_risk_check(event)
        if result is not None:
            return _attach_event_causality(result, event)
    if event.type is EventType.COLD_CHAIN_ALERT:
        return _attach_event_causality(run_cold_chain_cascade(event), event)
    return {
        "correlation_id": event.correlation_id,
        "scenario": None,
        "decision": None,
        "evidence": [],
        "trace": [],
        "status": "ignored",
    }


def _run_id_from_message(message: dict[str, Any]) -> str | None:
    event = message.get("event")
    if not isinstance(event, dict):
        return None
    return str(event.get("id") or event.get("correlation_id") or "") or None


def _attach_event_causality(result: dict[str, Any], event: Event) -> dict[str, Any]:
    result["correlation_id"] = event.correlation_id
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["caused_by"] = [event.correlation_id]
    return result
