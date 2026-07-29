"""Record one LLM call's provenance to the shared model-run registry.

Small, single-purpose module: `record_model_run` is used by three unrelated call sites
(`/inference/smoke`, `/chat`, and every agentic scenario route's `ExecutionContext`) that
don't otherwise share a module, so it gets its own rather than being attached to any one
of them - the same reasoning that kept `decision_governance.py` free of store I/O.
"""

from __future__ import annotations

import logging
from typing import Any

from shelfwise_mlops import ModelRun
from shelfwise_runtime import DataDomain

from .adaptive_attribution import (
    adaptive_attribution_enabled,
    build_failed_execution_trace,
    build_failed_model_trace,
    load_attribution_config,
    trajectory_family_for_event,
    trajectory_family_for_model_run,
)
from .state import model_run_registry, trace_registry

_LOGGER = logging.getLogger(__name__)


def record_model_run(payload: dict[str, Any]) -> None:
    normalized = dict(payload)
    normalized.setdefault("data_domain", DataDomain.WORLD_SIMULATION.value)
    stored = model_run_registry.record(ModelRun(**normalized))
    if not adaptive_attribution_enabled() or stored.status.lower() in {"ok", "success"}:
        return
    try:
        public = stored.to_dict()
        family = trajectory_family_for_model_run(public)
        references = trace_registry.successful_representations(
            tenant_id=stored.tenant_id,
            data_domain=stored.data_domain,
            trajectory_family=family,
        )
        trace_registry.put(
            build_failed_model_trace(
                public,
                references,
                config=load_attribution_config(),
            )
        )
    except Exception as exc:
        _LOGGER.warning(
            "failed model run retained without optional attribution (%s)",
            type(exc).__name__,
        )


def record_agentic_execution_failure(event: Any, *, failure_code: str) -> None:
    """Attach post-model orchestration failures to the existing correlated trace surface."""

    if not adaptive_attribution_enabled():
        return
    try:
        event_payload = (
            event.to_dict() if callable(getattr(event, "to_dict", None)) else dict(event)
        )
        tenant_id = str(event_payload.get("tenant_id") or "").strip()
        correlation_id = str(event_payload.get("correlation_id") or "").strip()
        data_domain = str(
            event_payload.get("data_domain") or DataDomain.WORLD_SIMULATION.value
        ).strip()
        model_runs = [
            run.to_dict()
            for run in model_run_registry.list(
                tenant_id=tenant_id,
                data_domain=data_domain,
                correlation_id=correlation_id,
            )
        ]
        family = (
            trajectory_family_for_model_run(model_runs[-1])
            if model_runs
            else trajectory_family_for_event(event_payload)
        )
        references = trace_registry.successful_representations(
            tenant_id=tenant_id,
            data_domain=data_domain,
            trajectory_family=family,
        )
        trace_registry.put(
            build_failed_execution_trace(
                model_runs,
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                data_domain=data_domain,
                trajectory_family=family,
                failure_code=failure_code,
                successful_representations=references,
                config=load_attribution_config(),
            )
        )
    except Exception as exc:
        _LOGGER.warning(
            "agentic failure retained without optional attribution (%s)",
            type(exc).__name__,
        )
