from __future__ import annotations

import pytest

from shelfwise_backend.app import (
    chat_store,
    cold_chain_demo,
    decision_store,
    event_bus,
    event_store,
    inbound_record_store,
    journal,
    learning_store,
    model_run_registry,
    product_catalog_store,
    prompt_registry,
    tenant_fact_store,
    tenant_profile_store,
    tool_audit,
    trace_registry,
    worldgen_run_store,
    write_limiter,
    writeback_sink,
)


@pytest.fixture(autouse=True)
def _reset_demo_stores() -> None:
    """Reset process-wide stores from shelfwise_backend.app.

    Decision ids are now deterministic per scenario (shelfwise_backend.cascade) instead of
    random per call - that's the fix for the duplicate-decision bug, not an oversight. It does
    mean repeated calls within a single process resolve to the SAME record, so tests need a
    clean slate each time to stay isolated from one another.
    """
    decision_store.clear()
    learning_store.clear()
    event_store.clear()
    inbound_record_store.clear()
    product_catalog_store.clear()
    event_bus.clear()
    journal.clear()
    trace_registry.clear()
    tool_audit.clear()
    model_run_registry.clear()
    prompt_registry.clear()
    tenant_fact_store.clear()
    tenant_profile_store.clear()
    writeback_sink.clear()
    worldgen_run_store.clear()
    cold_chain_demo.clear()
    chat_store.clear()
    write_limiter.configure(capacity=240, refill_per_s=8.0, max_keys=1024)
