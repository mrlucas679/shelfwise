"""Project canonical events into the digital twin store.

Second slice of `app.py`'s ~12-function ingest-adjacent core to move out (see
`HANDOFF.md`'s 2026-07-23 entry for the full call graph and the recommended sequencing).
This one is self-contained: it depends only on the shared `twin_service` singleton
(`state.py`) and reports its own fault-tolerant outcome, never raising past a real
projection failure into the caller (`_record_pipeline_event`, still in `app.py`, stays
durable-store-first regardless of whether the twin projection itself succeeds).
"""

from __future__ import annotations

import logging
from typing import Any

from shelfwise_contracts import Event
from shelfwise_runtime.provenance import DataDomainBoundaryError

from .state import twin_service

_LOGGER = logging.getLogger(__name__)


def project_twin_event(event: Event) -> dict[str, Any]:
    """Project a canonical event additively without breaking the existing cascade path."""
    try:
        results = twin_service.project_event(event)
    except DataDomainBoundaryError:
        return {
            "status": "skipped_non_operational",
            "event_id": event.id,
            "data_domain": event.data_domain.value,
            "reason": "non-operational events cannot enter the operational twin",
        }
    except Exception as exc:  # pragma: no cover - exercised by deployment fault drills
        _LOGGER.exception("twin projection failed for event %s: %s", event.id, str(exc)[:200])
        return {"status": "quarantined", "event_id": event.id, "reason": "projection_failed"}
    return {
        "status": "projected" if results else "ignored_no_store",
        "event_id": event.id,
        "observations": [result.to_dict() for result in results],
    }
