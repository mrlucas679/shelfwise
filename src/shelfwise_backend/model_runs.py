"""Record one LLM call's provenance to the shared model-run registry.

Small, single-purpose module: `record_model_run` is used by three unrelated call sites
(`/inference/smoke`, `/chat`, and every agentic scenario route's `ExecutionContext`) that
don't otherwise share a module, so it gets its own rather than being attached to any one
of them - the same reasoning that kept `decision_governance.py` free of store I/O.
"""

from __future__ import annotations

from typing import Any

from shelfwise_mlops import ModelRun
from shelfwise_runtime import DataDomain

from .state import model_run_registry


def record_model_run(payload: dict[str, Any]) -> None:
    normalized = dict(payload)
    normalized.setdefault("data_domain", DataDomain.WORLD_SIMULATION.value)
    model_run_registry.record(ModelRun(**normalized))
