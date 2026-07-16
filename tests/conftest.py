from __future__ import annotations

import os

import pytest

from shelfwise_edge import edge_device_registry

os.environ["SHELFWISE_TENANT_ID"] = "sa_retail_demo"

from shelfwise_backend.app import (
    candidate_store,
    chat_store,
    cold_chain_feed,
    decision_store,
    event_bus,
    event_store,
    inbound_record_store,
    inventory_position_store,
    journal,
    learning_store,
    model_run_registry,
    open_order_store,
    product_catalog_store,
    prompt_registry,
    tenant_fact_store,
    tenant_profile_store,
    tool_audit,
    trace_registry,
    twin_service,
    worldgen_run_store,
    write_limiter,
    writeback_sink,
)
from shelfwise_backend.state import scenario_engine


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
    inventory_position_store.clear()
    product_catalog_store.clear()
    event_bus.clear()
    journal.clear()
    trace_registry.clear()
    twin_service.store.clear()
    scenario_engine.clear()
    edge_device_registry.clear()
    tool_audit.clear()
    model_run_registry.clear()
    prompt_registry.clear()
    tenant_fact_store.clear()
    tenant_profile_store.clear()
    writeback_sink.clear()
    worldgen_run_store.clear()
    cold_chain_feed.clear()
    chat_store.clear()
    candidate_store.clear()
    open_order_store.clear()
    write_limiter.configure(capacity=240, refill_per_s=8.0, max_keys=1024)
