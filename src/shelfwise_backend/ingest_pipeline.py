"""Durable-store-first event ingest, cascade dispatch, and decision recording.

Third and final slice of `app.py`'s ~12-function ingest-adjacent core (see
`HANDOFF.md`'s 2026-07-23 entry for the full call graph, the reasoning behind extracting
it last, and the two tests any change here must be checked against before trusting the
full suite alone:
`tests/test_event_ingest.py::test_ingest_self_heals_when_the_crash_lands_after_publish_but_before_mark_published`
and `tests/test_decision_identity.py`.

This is the application's safety-critical spine: `record_pipeline_event` records an
event before publishing it (durable-store-first) specifically so a bus failure between
those two steps can never orphan the event - self-heal replays it safely on retry
because every downstream effect it triggers (`open_order_store.observe_event`,
`project_twin_event`, `cascade_for_event` -> `record_cascade`) is independently
idempotent. Do not change the order of operations in `record_pipeline_event` without
re-reading that comment and the crash-window test above.

Route-serving concerns that merely *consume* this pipeline (the scenario-drill wait
loop, deadline budgeting for HTTP responses) stay in `app.py` - they are about how a
route waits for and presents an outcome, not about the pipeline's own durability
guarantees, and moving them here would only recreate the tangle this extraction removes.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from shelfwise_contracts import Event
from shelfwise_runtime import DataDomain

from .decision_governance import attach_decision_governance
from .state import (
    cascade_dispatcher,
    decision_store,
    event_bus,
    event_store,
    inventory_position_store,
    open_order_store,
    trace_registry,
)
from .twin_projection import project_twin_event
from .worker import worker_enabled


def record_pipeline_event(event: Event) -> dict[str, Any]:
    # Recording before publishing (durable-store-first) means a bus failure between the two
    # calls would otherwise orphan the event: stored but never enqueued, with every retry of
    # the same event id short-circuited as a harmless-looking "duplicate". The `published`
    # flag closes that gap - only an event that actually reached the bus is treated as a true
    # duplicate on resubmission; anything recorded-but-unpublished self-heals by publishing
    # on the next attempt with the same id.
    is_new = event_store.record(event)
    if not is_new:
        stored = event_store.get(
            event.id,
            tenant_id=event.tenant_id,
            data_domain=event.data_domain,
        )
        if not _same_event_payload(stored, event):
            raise HTTPException(status_code=409, detail="Event id already has different content")
        if event_store.is_published(
            event.id,
            tenant_id=event.tenant_id,
            data_domain=event.data_domain,
        ):
            return {
                "status": "duplicate",
                "event": event.to_dict(),
                "bus_message_id": None,
                "cascade": None,
                "twin": {"status": "not_replayed"},
                "inventory": {"status": "not_replayed"},
            }
    open_order_store.observe_event(event)
    inventory_projection = inventory_position_store.project_event(event.to_dict())
    twin_projection = project_twin_event(event)

    bus_message_id = event_bus.publish(event)
    event_store.mark_published(
        event.id,
        tenant_id=event.tenant_id,
        data_domain=event.data_domain,
    )
    cascade = None if worker_enabled() else cascade_for_event(event)
    return {
        "status": "accepted",
        "event": event.to_dict(),
        "bus_message_id": bus_message_id,
        "cascade": cascade,
        "twin": twin_projection,
        "inventory": inventory_projection,
    }


def _same_event_payload(stored: dict[str, Any] | None, event: Event) -> bool:
    if stored is None:
        return False
    expected = event.to_dict()
    return all(stored.get(key) == value for key, value in expected.items())


def cascade_for_event(event: Event) -> dict[str, Any] | None:
    result = cascade_dispatcher.run(event)
    return record_cascade(result) if result is not None else None


def record_cascade(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("data_domain", DataDomain.WORLD_SIMULATION.value)
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision.setdefault("data_domain", result["data_domain"])
    attach_decision_governance(result)
    decision = result.get("decision")
    if isinstance(decision, dict) and decision.get("id"):
        result["decision"] = decision_store.upsert(decision)
    trace_registry.put(result)
    return result
