"""Shared singleton state for the ShelfWise backend.

Every service store, registry, and cross-cutting singleton the API routes depend on is
constructed exactly once here. This module holds pure object construction with no route
logic and no FastAPI dependency, so it can be imported by `app.py` and by future route
modules alike without either side owning "the" decision store, chat store, etc.
"""

from __future__ import annotations

from shelfwise_action import create_decision_store
from shelfwise_catalog import create_product_catalog_store
from shelfwise_connectors import (
    create_cursor_store,
    create_inbound_record_store,
    create_writeback_sink,
)
from shelfwise_inventory import create_inventory_position_store
from shelfwise_memory import create_learning_store
from shelfwise_mlops import (
    create_model_run_registry,
    create_prompt_registry,
    create_tenant_fact_store,
)
from shelfwise_storage import create_tenant_profile_store
from shelfwise_twin import (
    ScenarioEngine,
    TwinProjectionWorker,
    TwinService,
    create_scenario_branch_store,
    create_twin_service,
)
from shelfwise_worldgen import create_world_snapshot_store, create_worldgen_run_store

from .candidate_store import create_candidate_store
from .cascade_dispatcher import CascadeDispatcher
from .chat_store import create_chat_store
from .cold_chain_feed import ColdChainFeedService
from .event_bus import create_event_bus
from .event_store import create_event_store
from .open_orders import create_open_order_store
from .operational_facts import OperationalFactsProvider
from .tools.mcp_surface import AuditLog
from .trace import TraceRegistry
from .worker import CascadeWorker, WorkerLoopService, create_journal
from .world_facts import WorldFactsProvider

chat_store = create_chat_store()
decision_store = create_decision_store()
learning_store = create_learning_store()
event_store = create_event_store()
event_bus = create_event_bus()
journal = create_journal()
trace_registry = TraceRegistry()
tool_audit = AuditLog()
model_run_registry = create_model_run_registry()
prompt_registry = create_prompt_registry()
tenant_fact_store = create_tenant_fact_store()
tenant_profile_store = create_tenant_profile_store()
writeback_sink = create_writeback_sink()
inbound_record_store = create_inbound_record_store()
connector_cursor_store = create_cursor_store()
product_catalog_store = create_product_catalog_store()
candidate_store = create_candidate_store()
open_order_store = create_open_order_store()
inventory_position_store = create_inventory_position_store()
worldgen_run_store = create_worldgen_run_store()
world_snapshot_store = create_world_snapshot_store()
world_facts = WorldFactsProvider(world_snapshot_store)
twin_service: TwinService = create_twin_service()
scenario_engine = ScenarioEngine(twin_service, create_scenario_branch_store())
twin_projection_worker = TwinProjectionWorker(event_bus, twin_service)
cascade_dispatcher = CascadeDispatcher(
    world_facts=world_facts,
    twin_service=twin_service,
    product_catalog_store=product_catalog_store,
    inventory_position_store=inventory_position_store,
)
cascade_worker = CascadeWorker(
    bus=event_bus,
    journal=journal,
    decision_store=decision_store,
    handler=cascade_dispatcher.run_worker,
)
worker_service = WorkerLoopService(cascade_worker)
cold_chain_feed = ColdChainFeedService()


def operational_facts_for_query(
    tenant_id: str, *, store_id: str | None = None
) -> OperationalFactsProvider:
    """Return a live-only facts reader over the shared application stores."""
    return OperationalFactsProvider.for_query(
        tenant_id,
        twin_service=twin_service,
        product_catalog_store=product_catalog_store,
        inventory_position_store=inventory_position_store,
        store_id=store_id,
    )
