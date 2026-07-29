"""Demo/scenario-drill routes: deterministic and agentic cascade triggers, worldgen runs.

Fifth API router split out of `app.py`'s single-file route list, following the same
pattern `routes_twin.py`/`routes_catalog.py`/`routes_connectors.py`/`routes_mlops.py`
established. This is the largest and most intricate group extracted this session - read
`HANDOFF.md`'s 2026-07-23 entries before changing the idempotency-key logic in
`_demo_occurrence_suffix`: repeated clicks against a still-pending decision must reuse
its id, but a new occurrence after resolution must get a fresh one, and that invariant is
easy to break silently.

Two prerequisite helpers (`_request_timeout_seconds`, `_require_amd_inference`) moved to
`deps.py` and one (`_record_model_run`) to `model_runs.py` earlier in this same session
specifically to make this extraction possible without a circular import - see those
modules' docstrings.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from logging import getLogger
from time import monotonic, sleep
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from shelfwise_contracts import Event, EventType
from shelfwise_inference.orchestration import ExecutionMode
from shelfwise_runtime import DataDomain
from shelfwise_worldgen.scenarios import build as build_worldgen_scenario

from .agentic_cascade import (
    AgenticCascadeDeadlineError,
    AgenticCascadeError,
    ExecutionContext,
    run_catalog_price_check_via_agents,
    run_cold_chain_cascade_via_agents,
    run_expiry_risk_check_via_agents,
    run_golden_cascade_via_agents,
    run_procurement_cascade_via_agents,
    run_sales_cascade_via_agents,
)
from .cascade import (
    run_cold_chain_cascade,
    run_critic_rejection_cascade,
    run_golden_cascade,
    run_procurement_cascade,
    run_sales_cascade,
)
from .decision_governance import attach_decision_governance
from .deps import (
    CURRENT_TENANT_DEP,
    INGEST_AUTH_DEP,
    WRITE_LIMIT_DEP,
    _chat_data_domain,
    _is_production_deployment,
    _request_timeout_seconds,
    _require_amd_inference,
    write_path_guard,
)
from .ingest_pipeline import record_cascade, record_pipeline_event
from .model_runs import record_agentic_execution_failure, record_model_run
from .state import (
    decision_store,
    learning_store,
    operational_facts_for_query,
    tool_audit,
    world_facts,
    worldgen_run_store,
)
from .tenant import TenantContext
from .worker import worker_enabled

_LOGGER = getLogger(__name__)
router = APIRouter()

# Scenario POSTs create events and decisions. Keep their operational role requirement beside
# the shared write guard/rate limit; GET previews retain only the existing bounded access policy.
_SCENARIO_MUTATION_DEPS = [Depends(write_path_guard), WRITE_LIMIT_DEP, INGEST_AUTH_DEP]
_SCENARIO_PREVIEW_DEPS = [Depends(write_path_guard), WRITE_LIMIT_DEP]

_DEMO_DRILL_POLL_S = 0.2  # poll frequency while waiting (how often we look, not a bound)


def _agentic_unavailable(exc: AgenticCascadeError, *, event: Event) -> HTTPException:
    """Log provider diagnostics without exposing endpoint or credential details to clients."""
    record_agentic_execution_failure(event, failure_code="agentic_cascade_error")
    _LOGGER.warning("agentic inference unavailable: %s", str(exc)[:500])
    return HTTPException(status_code=503, detail="Live agentic inference is unavailable")


def _agentic_deadline_exceeded(
    exc: AgenticCascadeDeadlineError,
    *,
    event: Event,
) -> HTTPException:
    """Return a structured 503 when a cascade stops itself before the response deadline.

    This is the deliberate alternative to letting the request run past
    `_request_timeout_seconds()` and get killed by `enforce_request_deadline` - the cascade
    reports how far it got instead of leaving the caller with a bare timeout.
    """
    record_agentic_execution_failure(event, failure_code="agentic_deadline_exceeded")
    _LOGGER.warning("agentic cascade stopped before its deadline: %s", str(exc)[:500])
    return HTTPException(
        status_code=503,
        detail={
            "detail": "cascade could not finish inside the response deadline",
            "completed_model_calls": exc.completed_model_calls,
            "elapsed_ms": exc.elapsed_ms,
        },
    )


def _cascade_deadline() -> float:
    """Absolute monotonic deadline a cascade must stop calling models before."""
    return monotonic() + _request_timeout_seconds() - 1.0


def _production_execution_mode(requested_live: bool):
    """Force production agentic routes onto live AMD inference."""
    if _is_production_deployment():
        _require_amd_inference()
        return ExecutionMode.LIVE_REQUIRED
    return ExecutionMode.LIVE_REQUIRED if requested_live else ExecutionMode.OFFLINE_TEST


def _scenario_drill_wait_seconds() -> float:
    """How long a scenario drill route waits for the async worker before an honest 503.

    DERIVED from the request budget, not picked: the wait must be long enough to cover
    real queue depth (a fixed 15s guess starves under load) while still answering
    before the deadline middleware's 504 pre-empts the route's own honest 503 - so it
    is the request budget minus a response margin, overridable via
    SHELFWISE_SCENARIO_DRILL_WAIT_SECONDS but always capped below the budget.
    """
    budget = float(_request_timeout_seconds())
    ceiling = max(1.0, budget - 10.0)
    raw = os.getenv("SHELFWISE_SCENARIO_DRILL_WAIT_SECONDS", "").strip()
    try:
        configured = float(raw) if raw else ceiling
    except ValueError:
        configured = ceiling
    # Shorter is always safe here (an early 503 is honest and retryable; no work is
    # stolen) - only the ceiling is load-bearing, so clamp up is not needed.
    return min(max(0.1, configured), ceiling)


def _await_worker_cascade(event: Any, ctx: TenantContext) -> dict[str, Any]:
    """Wait for the async worker to actually process a just-published event.

    In the real production topology (`WORKER_ENABLED=true`), a cascade is never computed
    synchronously inline - `record_pipeline_event` deliberately defers it to the queue
    consumer, same as it would for a real recall or shrink count arriving from a source
    system. A "drill" endpoint that immediately checked `cascade is None` and 500'd was a
    leftover single-process demo assumption; the honest real-app behavior is to wait for
    the worker (it polls every 0.25s) and return the decision it actually produces, or a
    truthful still-processing signal if the wait bound is exceeded - never a fabricated
    failure for a submission that in fact succeeded.
    """
    deadline = monotonic() + _scenario_drill_wait_seconds()
    while monotonic() < deadline:
        for row in decision_store.list():
            caused_by = row.get("caused_by")
            if isinstance(caused_by, (list, tuple)) and event.id in caused_by:
                if str(row.get("tenant_id") or ctx.tenant_id) != ctx.tenant_id:
                    continue
                return record_cascade({"decision": row})
        sleep(_DEMO_DRILL_POLL_S)
    raise HTTPException(
        status_code=503,
        detail=(
            "event was accepted and queued but the worker has not produced a decision "
            "yet; retry shortly"
        ),
    )


def _resolve_demo_pipeline_cascade(
    outcome: dict[str, Any], event: Any, ctx: TenantContext
) -> dict[str, Any]:
    """Return the real cascade for a pipeline-routed demo event, sync or async."""
    cascade = outcome.get("cascade")
    if isinstance(cascade, dict):
        return cascade
    if outcome.get("status") == "duplicate":
        # A repeat drill click resubmits the same deterministic event id - the original
        # decision already exists (or is still being produced by the worker); look it up
        # instead of treating a legitimate idempotent resubmission as a hard failure.
        for row in decision_store.list():
            caused_by = row.get("caused_by")
            if (
                isinstance(caused_by, (list, tuple))
                and event.id in caused_by
                and str(row.get("tenant_id") or ctx.tenant_id) == ctx.tenant_id
            ):
                return record_cascade({"decision": row})
    return _await_worker_cascade(event, ctx)


def _demo_occurrence_suffix(
    key: str, *, id_prefix: str, tenant_id: str, data_domain: str = "world_simulation"
) -> str:
    """Resolve a stable-but-not-stuck suffix for a demo trigger keyed by (tenant, type, sku, day).

    Repeated clicks against a still-pending decision for this key must reuse its id (upsert in
    place) so the approval queue does not grow one identical duplicate card per click. But once
    that decision is resolved (approved/rejected), a further trigger for the same key is a
    genuinely new occurrence and must get a new id, not resurrect the resolved one. We walk an
    occurrence counter and stop at the first slot that is either free or still pending.
    """
    occurrence = 0
    while True:
        suffix = hashlib.sha256(f"{key}:{occurrence}".encode()).hexdigest()[:12]
        decision_id = f"dec_{_slug_tenant(tenant_id)}_{data_domain}_{id_prefix}_{suffix}"
        existing = decision_store.get(decision_id)
        if existing is None or (existing.get("status") or "pending").lower() == "pending":
            return suffix
        occurrence += 1


def _slug_tenant(value: str) -> str:
    """Keep demo lookup IDs aligned with cascade replay IDs."""
    clean = "".join(char if char.isalnum() else "_" for char in value.strip().lower())
    return clean.strip("_") or "local"


def _demo_run_scope(value: str | None) -> str:
    """Return a bounded key suffix for an explicitly scoped automated demo run."""
    if not value or not value.strip():
        return ""
    digest = hashlib.sha256(value.strip().encode()).hexdigest()[:12]
    return f":run:{digest}"


def _demo_occurrence_ts(today: date) -> datetime:
    """Return a deterministic timestamp for a day-scoped, idempotent demo trigger.

    `id`/`correlation_id` for these events are already deterministic per (tenant, type,
    sku, day) so a resubmission of the same logical trigger reuses the same id - that is
    the documented idempotency contract (`_demo_occurrence_suffix`). But `Event.ts` was
    generated fresh via `datetime.now(UTC)` on every call, so `_same_event_payload` saw a
    different timestamp on any resubmission more than an instant apart and rejected it with
    409 "different content" - a legitimate repeat click or webhook retry, not a real
    conflict. Pinning `ts` to midnight UTC of the same day keeps it inside the existing
    day-bucket the key is already scoped to, so a real resubmission is byte-identical.
    """
    return datetime(today.year, today.month, today.day, tzinfo=UTC)


def _demo_event(ctx: TenantContext, event_type, *, variant: str = "deterministic"):
    """Create a tenant-owned trigger from the tenant's generated world facts.

    The id is derived deterministically from (tenant, event type, sku, day) rather than a
    random uuid - see `_demo_occurrence_suffix` for why repeated clicks dedupe while a new
    occurrence after resolution still gets a fresh id.
    """

    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    supplier = world_facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today = datetime.now(UTC).date().isoformat()
    variant_slug = _slug_tenant(variant)
    legacy = variant_slug == "deterministic"
    scope = "" if legacy else f":{variant_slug}"
    prefix_scope = "" if legacy else f"_{variant_slug}"
    key = f"{ctx.tenant_id}{scope}:{event_type.value}:{scenario.sku}:{today}"
    id_prefix = f"evt_demo{prefix_scope}_{event_type.value}"
    suffix = _demo_occurrence_suffix(key, id_prefix=id_prefix, tenant_id=ctx.tenant_id)
    return Event(
        id=f"{id_prefix}_{suffix}",
        type=event_type,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=(
            f"demo_{event_type.value}_{suffix}"
            if legacy
            else f"demo_{variant_slug}_{event_type.value}_{suffix}"
        ),
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "supplier": str(supplier["name"]).lower(),
            "site_id": scenario.location,
        },
    )


def _reject_operational_domain_for_synthetic_drill(data_domain: str | None, *, drill: str) -> None:
    """Fail closed when a synthetic-anomaly drill is pointed at real twin data."""

    if data_domain == DataDomain.OPERATIONAL_TWIN.value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The {drill} drill fabricates a synthetic anomaly and is "
                "simulation-only; live operational anomalies enter through the real "
                "ingest pipeline, never through a drill projected onto twin data."
            ),
        )


def _agentic_cascade_context(
    ctx: TenantContext,
    event_type,
    *,
    data_domain: Literal["operational_twin", "world_simulation"] | None,
    store_id: str | None,
):
    """Resolve the (facts, trigger event) pair an agentic demo route reasons over.

    World-simulation stays the existing `_demo_event`/`world_facts` path unchanged. Operational
    reads the same reported twin state the Critic/Executive tools already know how to consume
    (`OperationalFactsProvider` implements the same `get_scenario_facts`/`get_supplier_for_sku`
    contract `WorldFactsProvider` does), so no cascade math or tool code needs to branch on
    domain - only which facts object is handed to it.
    """

    resolved_domain = data_domain or _chat_data_domain()
    if resolved_domain == DataDomain.WORLD_SIMULATION.value:
        return world_facts, _demo_event(ctx, event_type, variant="agentic")

    facts = operational_facts_for_query(ctx.tenant_id, store_id=store_id)
    scenario = facts.get_scenario_facts(ctx.tenant_id)
    supplier = facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:{event_type.value}:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key,
        id_prefix=f"evt_operational_{event_type.value}",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.OPERATIONAL_TWIN.value,
    )
    event = Event(
        id=f"evt_operational_{event_type.value}_{suffix}",
        type=event_type,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.OPERATIONAL_TWIN,
        correlation_id=f"operational_{event_type.value}_{suffix}",
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "store_id": store_id or scenario.location,
            "supplier": str(supplier["name"]).lower(),
            "site_id": scenario.location,
        },
    )
    return facts, event


def _demo_catalog_price_event(ctx: TenantContext):
    """Create a generated-world POS price exception for the agentic guardrail route."""

    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:catalog_price_agentic:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_catalog_price_agentic", tenant_id=ctx.tenant_id
    )
    observed = scenario.unit_price * Decimal("1.20")
    return Event(
        id=f"evt_demo_catalog_price_agentic_{suffix}",
        type=EventType.SALE,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_catalog_price_agentic_{suffix}",
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "units": 2,
            "unit_price_cents": observed.minor_units,
            "catalog_price_cents": scenario.unit_price.minor_units,
        },
    )


def _demo_expiry_risk_event(ctx: TenantContext):
    """Create a generated-world imminent-expiry event for the agentic guardrail route."""

    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:expiry_risk_agentic:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_expiry_risk_agentic", tenant_id=ctx.tenant_id
    )
    return Event(
        id=f"evt_demo_expiry_risk_agentic_{suffix}",
        type=EventType.EXPIRY_ENTRY,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_expiry_risk_agentic_{suffix}",
        payload={
            "sku": scenario.sku,
            "batch_id": f"BATCH-{scenario.sku}",
            "category": scenario.category,
            "location": scenario.location,
            "days_to_expiry": 1,
        },
    )


def _preview_demo_cascade(result: dict[str, Any]) -> dict[str, Any]:
    """Enrich a read-only demo preview without mutating stores or traces."""
    attach_decision_governance(result)
    return result


def _assign_result_tenant(result: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["tenant_id"] = tenant_id
    result["tenant_id"] = tenant_id
    return result


def _stride_sample(events: list, limit: int) -> list:
    """Take an evenly spaced, chronological sample across the WHOLE event stream.

    Taking the first N events instead starves the pipeline: the world emits every
    product's 08:00 stock update before its first sale of the day, so with a large
    assortment the window fills with a single event type and the sales/expiry
    cascades never see one event. Deterministic: same stream + limit, same sample.
    """
    if len(events) <= limit:
        return events
    step = len(events) / limit
    return [events[int(index * step)] for index in range(limit)]


def _pipeline_summary(outcome: dict[str, Any]) -> dict[str, object]:
    event = outcome["event"] if isinstance(outcome.get("event"), dict) else {}
    cascade = outcome.get("cascade")
    decision = cascade.get("decision") if isinstance(cascade, dict) else None
    return {
        "status": outcome.get("status"),
        "bus_message_id": outcome.get("bus_message_id"),
        "id": event.get("id"),
        "type": event.get("type"),
        "ts": event.get("ts"),
        "tenant_id": event.get("tenant_id"),
        "cascade_scenario": cascade.get("scenario") if isinstance(cascade, dict) else None,
        "decision_id": decision.get("id") if isinstance(decision, dict) else None,
    }


def _worldgen_cold_chain_alert(
    *,
    scenario_id: str,
    seed: int,
    tenant_id: str,
    actor: str,
    area: str,
    schedule: list[dict[str, Any]],
):

    scenario = world_facts.get_scenario_facts(tenant_id)
    first_window = schedule[0] if schedule else {}
    alert_ts = str(first_window.get("end") or first_window.get("start") or "2026-06-23T10:00:00")
    stage = int(first_window.get("stage") or 4)
    measured_outage_hours = max(Decimal(stage) / Decimal("2"), Decimal("2.5"))
    return Event.parse_wire(
        {
            "id": f"evt_{scenario_id}_{seed}_cold_chain_alert",
            "type": EventType.COLD_CHAIN_ALERT.value,
            "ts": alert_ts,
            "actor": actor,
            "source": "api",
            "tenant_id": tenant_id,
            "data_domain": DataDomain.WORLD_SIMULATION.value,
            "correlation_id": f"worldgen:{scenario_id}:{seed}:cold_chain",
            "payload": {
                "site_id": actor,
                "area": area,
                "asset_id": f"cold-chain:{actor}:{scenario.category}",
                "category": scenario.category,
                "diagnosis": "generator_failed",
                "severity": 2,
                "predicted_minutes_to_unsafe": "18",
                "measured_outage_hours": str(measured_outage_hours),
                "temp_c": "8.2",
                "stock_at_risk": (scenario.unit_price * scenario.units_on_hand).to_dict(),
                "synthetic": True,
            },
        }
    )


@router.get("/scenarios/worldgen-runs")
def list_worldgen_runs(
    limit: int = 100,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        runs = worldgen_run_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"runs": runs}


@router.get("/scenarios/worldgen-runs/{run_id}")
def get_worldgen_run(run_id: str, ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    run = worldgen_run_store.get(run_id, tenant_id=ctx.tenant_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Worldgen run not found")
    return {"run": run}


@router.post("/scenarios/golden", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_golden(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:

    return record_cascade(run_golden_cascade(_demo_event(ctx, EventType.SCAN), facts=world_facts))


@router.post("/scenarios/recall", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_recall(
    run_scope: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Drive a generated-world supplier recall through the real event and HITL pipeline."""

    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    supplier = world_facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    key = f"{ctx.tenant_id}:recall:{scenario.sku}:{today}{_demo_run_scope(run_scope)}"
    suffix = _demo_occurrence_suffix(key, id_prefix="evt_demo_recall", tenant_id=ctx.tenant_id)
    event = Event(
        id=f"evt_demo_recall_{suffix}",
        type=EventType.RECALL_NOTICE,
        ts=_demo_occurrence_ts(today_date),
        actor=f"supplier_{supplier['supplier_id']}",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_recall_{suffix}",
        payload={
            "recall_id": f"REC-DEMO-{suffix}",
            "sku": scenario.sku,
            "lot_id": f"LOT-{scenario.sku}",
            "units": max(1, scenario.units_on_hand // 4),
            "location": scenario.location,
            "reason": "possible cold-chain contamination",
            "issued_by": f"{supplier['name']} Quality",
            "issuer_verified": True,
        },
    )
    outcome = record_pipeline_event(event)
    return _resolve_demo_pipeline_cascade(outcome, event, ctx)


@router.post("/scenarios/inventory-exception", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_inventory_exception(
    run_scope: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Drive a generated-world shrink count through the real event and HITL pipeline."""

    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    key = f"{ctx.tenant_id}:inventory_exception:{scenario.sku}:{today}{_demo_run_scope(run_scope)}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_inventory_exception", tenant_id=ctx.tenant_id
    )
    counted_units = max(0, scenario.units_on_hand - max(1, scenario.units_on_hand // 10))
    event = Event(
        id=f"evt_demo_inventory_exception_{suffix}",
        type=EventType.INVENTORY_EXCEPTION,
        ts=_demo_occurrence_ts(today_date),
        actor="cycle_count_team",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_inventory_exception_{suffix}",
        payload={
            "exception_id": f"EXC-DEMO-{suffix}",
            "exception_type": "shrink",
            "sku": scenario.sku,
            "reason": "cycle count below system stock",
            "location": scenario.location,
            "expected_units": scenario.units_on_hand,
            "counted_units": counted_units,
            "count_reference": f"COUNT-{suffix}",
        },
    )
    outcome = record_pipeline_event(event)
    return _resolve_demo_pipeline_cascade(outcome, event, ctx)


@router.get("/scenarios/golden", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_golden_get() -> dict[str, object]:
    return _preview_demo_cascade(run_golden_cascade(facts=world_facts))


@router.post("/scenarios/golden/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_golden_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the golden scenario's Critic/Executive verdicts through a real Gemma tool loop.

    Unlike /scenarios/golden (deterministic math + hand-authored evidence), this route requires
    an actual model call and tool-calling round trip. With live_required=true (default) it
    hard-fails with 503 instead of silently falling back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the Critic/Executive tool calls in reported twin
    state (via `OperationalFactsProvider`) instead of the generated world - the same real
    facts contract `product_attention`/`product_search` already use, applied to the agentic
    tool-calling path for the first time. Requires onboarded twin data for this tenant/store;
    raises 422 if the twin cannot yet answer (see `MissingOperationalFacts`).
    """

    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SCAN, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_golden_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    return record_cascade(result)


@router.post("/scenarios/procurement/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_procurement_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the procurement reorder/supplier verdicts through a real Gemma tool loop.

    Unlike /scenarios/procurement (deterministic math + hand-authored evidence), this route
    requires an actual model call and tool-calling round trip over get_reorder_policy and
    get_supplier_ranking. With live_required=true (default) it hard-fails with 503 instead
    of silently falling back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state
    (`OperationalFactsProvider`), the same contract the golden agentic route uses;
    raises 422 when the twin cannot yet answer for this tenant/store.
    """

    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SUPPLIER_UPDATE, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_procurement_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    return record_cascade(result)


@router.post("/scenarios/sales/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_sales_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the POS price-integrity verdict through a real Gemma tool loop.

    Unlike /scenarios/sales (deterministic math + hand-authored evidence), this route requires
    an actual model call and tool-calling round trip over check_price_integrity. With
    live_required=true (default) it hard-fails with 503 instead of silently falling back
    to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state; raises
    422 when the twin cannot yet answer for this tenant/store.
    """

    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SALE, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_sales_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    return record_cascade(result)


@router.post("/scenarios/catalog-price/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_catalog_price_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the POS catalogue-price guardrail through a real Gemma tool loop.

    Simulation-only by contract: this drill fabricates a synthetic price outlier to
    demonstrate the guardrail, and fabricating an anomaly from a real store's twin data
    would be invented telemetry. Live operational price exceptions enter through the
    real POS/ingest pipeline, where the catalog-price dispatcher already screens every
    sale.
    """
    _reject_operational_domain_for_synthetic_drill(data_domain, drill="catalog-price")
    mode = _production_execution_mode(live_required)
    event = _demo_catalog_price_event(ctx)
    try:
        result = run_catalog_price_check_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=world_facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    if result is None:
        raise HTTPException(status_code=500, detail="Catalog-price demo did not produce a decision")
    return record_cascade(result)


@router.post("/scenarios/expiry-risk/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_expiry_risk_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the imminent-expiry guardrail through a real Gemma tool loop.

    Simulation-only by contract, for the same reason as the catalog-price drill: the
    synthetic near-expiry entry it fabricates must never be projected onto real twin
    data. Live expiry entries arrive through the real ingest pipeline's expiry-risk
    dispatcher.
    """
    _reject_operational_domain_for_synthetic_drill(data_domain, drill="expiry-risk")
    mode = _production_execution_mode(live_required)
    event = _demo_expiry_risk_event(ctx)
    try:
        result = run_expiry_risk_check_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=world_facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    if result is None:
        raise HTTPException(status_code=500, detail="Expiry-risk demo did not produce a decision")
    return record_cascade(result)


@router.post("/scenarios/cold-chain/agentic", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_cold_chain_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the cold-chain facilities-escalation verdict through a real Gemma tool loop.

    Unlike /scenarios/cold-chain (deterministic math + hand-authored evidence), this route
    requires an actual model call and tool-calling round trip over get_cold_chain_status.
    With live_required=true (default) it hard-fails with 503 instead of silently falling
    back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state; raises
    422 when the twin cannot yet answer for this tenant/store.
    """

    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.COLD_CHAIN_ALERT, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_cold_chain_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            execution=ExecutionContext(
                audit=tool_audit,
                model_run_recorder=record_model_run,
                deadline=_cascade_deadline(),
            ),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc, event=event) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc, event=event) from exc
    return record_cascade(result)


@router.post("/scenarios/critic-rejection", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_critic_rejection(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return record_cascade(
        _assign_result_tenant(run_critic_rejection_cascade(facts=world_facts), ctx.tenant_id)
    )


@router.get("/scenarios/critic-rejection", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_critic_rejection_get() -> dict[str, object]:
    return _preview_demo_cascade(run_critic_rejection_cascade(facts=world_facts))


@router.post("/scenarios/procurement", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_procurement(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:

    return record_cascade(
        run_procurement_cascade(_demo_event(ctx, EventType.SUPPLIER_UPDATE), facts=world_facts)
    )


@router.get("/scenarios/procurement", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_procurement_get() -> dict[str, object]:
    return _preview_demo_cascade(run_procurement_cascade(facts=world_facts))


@router.post("/scenarios/sales", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_sales(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:

    return record_cascade(run_sales_cascade(_demo_event(ctx, EventType.SALE), facts=world_facts))


@router.get("/scenarios/sales", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_sales_get() -> dict[str, object]:
    return _preview_demo_cascade(run_sales_cascade(facts=world_facts))


@router.post("/scenarios/cold-chain", dependencies=_SCENARIO_MUTATION_DEPS)
def demo_cold_chain(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:

    return record_cascade(
        run_cold_chain_cascade(_demo_event(ctx, EventType.COLD_CHAIN_ALERT), facts=world_facts)
    )


@router.get("/scenarios/cold-chain", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_cold_chain_get() -> dict[str, object]:
    return _preview_demo_cascade(run_cold_chain_cascade(facts=world_facts))


@router.get("/scenarios/worldgen/{scenario_id}", dependencies=_SCENARIO_PREVIEW_DEPS)
def demo_worldgen_drill(
    scenario_id: str,
    limit: int = 80,
    seed_override: int | None = None,
    assortment_size: int | None = None,
    catalog_scale: str = "supermarket",
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    if assortment_size is not None and not (0 < assortment_size <= 20_000):
        raise HTTPException(status_code=422, detail="assortment_size must be between 1 and 20000")
    try:
        world, schedule = build_worldgen_scenario(
            scenario_id,
            seed_override=seed_override,
            assortment_size=assortment_size,
            catalog_scale=catalog_scale,
            tenant_id=ctx.tenant_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scenario not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run_id = f"worldrun_{uuid4().hex[:12]}"
    records: list[dict[str, object]] = []
    cascades: list[dict[str, Any]] = []
    stream = list(world.run())
    for event in _stride_sample(stream, limit):
        outcome = record_pipeline_event(event)
        records.append(_pipeline_summary(outcome))
        cascade = outcome.get("cascade")
        if isinstance(cascade, dict):
            cascades.append(cascade)

    alert = _worldgen_cold_chain_alert(
        scenario_id=scenario_id,
        seed=world.cfg.seed,
        tenant_id=world.cfg.tenant_id,
        actor=world.cfg.store_id,
        area=world.cfg.area,
        schedule=schedule,
    )
    alert_outcome = record_pipeline_event(alert)
    records.append(_pipeline_summary(alert_outcome))
    alert_cascade = alert_outcome.get("cascade")
    if isinstance(alert_cascade, dict):
        cascades.append(alert_cascade)

    decisions = [
        cascade["decision"] for cascade in cascades if isinstance(cascade.get("decision"), dict)
    ]
    run = worldgen_run_store.record(
        {
            "run_id": run_id,
            "tenant_id": world.cfg.tenant_id,
            "scenario_id": scenario_id,
            "seed": world.cfg.seed,
            "status": "completed",
            "synthetic": True,
            "worker_enabled": worker_enabled(),
            "events_total": len(records),
            "events_accepted": sum(1 for item in records if item["status"] == "accepted"),
            "duplicates": sum(1 for item in records if item["status"] == "duplicate"),
            "decisions_total": len(decisions),
            "pending_total": sum(1 for item in decisions if item.get("status") == "pending"),
            "event_ids": [str(item["id"]) for item in records],
            "decision_ids": [str(decision["id"]) for decision in decisions],
            "cascade_scenarios": [
                str(cascade.get("scenario"))
                for cascade in cascades
                if cascade.get("scenario") is not None
            ],
            "schedule_sample": schedule[:5],
        }
    )
    return {
        "run": run,
        "scenario_id": scenario_id,
        "synthetic": True,
        "worker_enabled": worker_enabled(),
        "stream_events_total": len(stream),
        "events_total": len(records),
        "events_accepted": sum(1 for item in records if item["status"] == "accepted"),
        "duplicates": sum(1 for item in records if item["status"] == "duplicate"),
        "schedule_sample": schedule[:5],
        "events": records,
        "cascades": cascades,
        "decisions": decisions,
    }
