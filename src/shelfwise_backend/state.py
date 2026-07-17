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
from shelfwise_mlops.skill_registry import create_skill_registry, default_skill_manifests
from shelfwise_storage import create_tenant_profile_store
from shelfwise_twin import (
    ScenarioEngine,
    TwinProjectionLoopService,
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
from .conversation_memory import create_conversation_memory_store
from .event_bus import create_event_bus, stale_consumer_idle_ms
from .event_store import create_event_store
from .governed_execution import FidelityRevalidationService, build_capability_registry
from .open_orders import create_open_order_store
from .operational_facts import OperationalFactsProvider
from .tools.mcp_surface import AuditLog
from .trace import TraceRegistry
from .worker import CascadeWorker, WorkerLoopService, create_journal
from .worker.plans import PlanRunner as _PlanRunner
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
# Wired as a supplement to inline ingest projection (projection is idempotent):
# recovers events whose inline projection was lost to a crash and consumes on replicas
# that never saw the original request. Starts only when TWIN_PROJECTION_WORKER_ENABLED
# is set AND the bus is Redis - see TwinProjectionLoopService for why memory refuses.
twin_projection_service = TwinProjectionLoopService(
    twin_projection_worker,
    reclaim_idle_ms=stale_consumer_idle_ms(),
)
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
conversation_memory_store = create_conversation_memory_store()
skill_registry = create_skill_registry()

# Governed plan execution: the registry carries ONLY real capabilities (the HITL
# write-back sink as the sole write; twin fidelity recompute as the read), and the
# runner journals every step. The revalidation service turns "multi-week fidelity
# re-validation" into a running schedule instead of a waiting roadmap line.
capability_registry = build_capability_registry(
    writeback_sink=writeback_sink, twin_service=twin_service
)


async def _publish_plan_progress(kind: str, payload: dict) -> None:
    import logging

    logging.getLogger("shelfwise.plans").info("plan_%s %s", kind, payload)


plan_runner = _PlanRunner(capability_registry, journal, _publish_plan_progress)
fidelity_revalidation_service = FidelityRevalidationService(
    runner=plan_runner, twin_service=twin_service, writeback_sink=writeback_sink
)


def _seed_platform_skills() -> None:
    """Register the built-in promoted catalogue against the real tool/agent surface.

    Seeding is idempotent (registry upsert by id) and validated: a manifest naming a
    tool or agent that does not actually exist fails loudly at process start, never
    silently at conversation time.
    """
    from shelfwise_contracts import AgentName

    known_agents = {agent.value for agent in AgentName}
    known_tools = {
        "get_stock",
        "get_demand_forecast",
        "get_expiry_risk",
        "simulate_markdown",
        "get_cold_chain_status",
        "get_reorder_policy",
        "get_supplier_ranking",
        "check_price_integrity",
        "get_delivery_status",
        "get_stock_sourcing_options",
        "get_thresholds",
        "list_open_decisions",
    }
    for manifest in default_skill_manifests():
        skill_registry.upsert(manifest, known_agents=known_agents, known_tools=known_tools)


_seed_platform_skills()


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
