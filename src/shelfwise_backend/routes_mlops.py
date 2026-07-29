"""MLOps governance routes: model runs, prompts, accountability, observability, skills,
mined-playbook activation/promotion/retirement, and governed plan execution.

Fourth API router split out of `app.py`'s single-file route list, following the same
pattern `routes_twin.py`/`routes_catalog.py`/`routes_connectors.py` established. Depends
on `state.py` singletons, the already-extracted `decision_access.py`/`decision_governance.py`
helpers, and `deps.py`'s `_auth_mode`/`_chat_data_domain` - no cross-talk with the ingest
pipeline or the demo/scenario routes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from shelfwise_backend.worker.plans import Plan
from shelfwise_mlops import (
    OutcomeRecord,
    SkillStats,
    build_accountability_report,
    draft_skills,
)
from shelfwise_mlops import activate as activate_skill
from shelfwise_mlops import to_plan as skill_to_plan
from shelfwise_mlops.skill_registry import promote as promote_skill_manifest
from shelfwise_mlops.skill_registry import retire as retire_skill_manifest
from shelfwise_runtime import DataDomain

from .decision_access import decision_action, decision_tenant_id, tenant_scoped_decisions
from .decision_governance import inference_rate
from .deps import (
    APPROVAL_AUTH_DEP,
    CURRENT_TENANT_DEP,
    WORKER_AUTH_DEP,
    WRITE_LIMIT_DEP,
    _auth_mode,
    _chat_data_domain,
    write_path_guard,
)
from .observability import build_observability_snapshot
from .state import (
    candidate_store,
    decision_store,
    evaluation_registry,
    event_bus,
    event_store,
    inbound_record_store,
    journal,
    learning_store,
    model_run_registry,
    open_order_store,
    plan_runner,
    prompt_registry,
    skill_registry,
    tenant_fact_store,
    worker_service,
    writeback_sink,
)
from .tenant import TenantContext
from .worker import MemoryConsolidationWorker

router = APIRouter()


@router.get("/mlops/model-runs")
def list_model_runs(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    resolved_domain = data_domain or _chat_data_domain()
    runs = model_run_registry.list(
        tenant_id=tenant_id,
        data_domain=resolved_domain,
    )
    return {
        "data_domain": resolved_domain,
        "model_runs": [run.to_dict() for run in runs],
    }


@router.get("/mlops/prompts")
def list_prompt_versions(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    prompts = prompt_registry.list(tenant_id=tenant_id)
    return {"prompt_versions": [prompt.to_dict() for prompt in prompts]}


@router.get("/mlops/accountability")
def accountability_report(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    # Derive the tenant from the authenticated context, never a caller-supplied query
    # param - accepting an arbitrary tenant_id here let any authenticated caller read
    # another tenant's model-run and decision accountability data.
    resolved_domain = data_domain or _chat_data_domain()
    runs = model_run_registry.list(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
    )
    report = build_accountability_report(
        tenant_id=ctx.tenant_id,
        decisions=tenant_scoped_decisions(ctx, data_domain=resolved_domain),
        models_used=[run.model for run in runs],
        prompt_versions=[run.prompt_version for run in runs],
    )
    return {
        "data_domain": resolved_domain,
        "report": report.to_dict(),
        "markdown": report.to_markdown(),
    }


@router.get("/mlops/observability")
def observability_snapshot(
    limit: int = 500,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    resolved_domain = data_domain or _chat_data_domain()
    try:
        events = event_store.list(
            limit=limit,
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        )
        inbound_records = (
            inbound_record_store.list(tenant_id=ctx.tenant_id, limit=limit)
            if resolved_domain == DataDomain.OPERATIONAL_TWIN.value
            else []
        )
        candidate_records = candidate_store.list(
            ctx.tenant_id,
            data_domain=resolved_domain,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = build_observability_snapshot(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
        decisions=tenant_scoped_decisions(ctx, data_domain=resolved_domain),
        model_runs=[
            run.to_dict()
            for run in model_run_registry.list(
                tenant_id=ctx.tenant_id,
                data_domain=resolved_domain,
            )
        ],
        inbound_records=inbound_records,
        events=events,
        bus_stats={
            **event_bus.stats(tenant_id=ctx.tenant_id),
            "scope": "tenant_all_domains",
        },
        writeback_tasks=writeback_sink.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        worker_status=worker_service.status(),
        worker_runs=journal.list_runs(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        learning_events=learning_store.list_events(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        tenant_facts=tenant_fact_store.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
            active_only=False,
        ),
        rate_zar_per_1k=inference_rate(),
        candidate_records=candidate_records,
        open_orders=open_order_store.list(
            ctx.tenant_id,
            data_domain=resolved_domain,
            limit=limit,
        ),
    )
    return {"data_domain": resolved_domain, "snapshot": snapshot}


@router.get("/mlops/tenant-facts")
def list_tenant_facts(
    include_tombstoned: bool = False,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    facts = tenant_fact_store.list(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
        active_only=not include_tombstoned,
    )
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": resolved_domain,
        "facts": facts,
    }


def _learning_outcome_records(tenant_id: str, data_domain: str) -> list[OutcomeRecord]:
    decisions = {
        str(decision.get("id")): decision
        for decision in decision_store.list()
        if decision_tenant_id(decision, tenant_id) == tenant_id
        and str(decision.get("data_domain") or DataDomain.WORLD_SIMULATION.value) == data_domain
    }
    records: list[OutcomeRecord] = []
    for event in learning_store.list_events(
        tenant_id=tenant_id,
        data_domain=data_domain,
    ):
        decision_id = str(event.get("decision_id") or "")
        decision = decisions.get(decision_id)
        if decision is None:
            continue
        action = decision_action(decision)
        outcome = event.get("outcome") if isinstance(event.get("outcome"), dict) else {}
        try:
            success_score = Decimal(str(outcome.get("success_score") or "0"))
        except (TypeError, ValueError, InvalidOperation):
            success_score = Decimal("0")
        records.append(
            OutcomeRecord(
                tenant_id=tenant_id,
                sku=str(event.get("sku") or action.get("sku") or "unknown"),
                action=str(action.get("type") or "unknown"),
                success_score=success_score,
                evidence_refs=_memory_evidence_refs(decision_id, decision),
                data_domain=data_domain,
            )
        )
    return records


def _memory_consolidation_worker() -> MemoryConsolidationWorker:
    return MemoryConsolidationWorker(
        journal=journal,
        fact_store=tenant_fact_store,
        records_for_tenant=_learning_outcome_records,
    )


def _memory_evidence_refs(decision_id: str, decision: dict[str, Any]) -> tuple[str, ...]:
    refs = [decision_id]
    caused_by = decision.get("caused_by")
    if isinstance(caused_by, list):
        refs.extend(str(item) for item in caused_by if item)
    return tuple(dict.fromkeys(refs))


@router.post(
    "/mlops/consolidate-memory",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def consolidate_memory(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = WORKER_AUTH_DEP,
) -> dict[str, object]:
    return _memory_consolidation_worker().process_tenant(
        ctx.tenant_id,
        data_domain=data_domain or _chat_data_domain(),
    )


# Plan-step templates for playbooks mined from outcome history. Capabilities name the
# same governed action types the HITL/writeback path already uses; a compiled plan is a
# governed RECOMMENDATION artifact (like a writeback task), never an autonomous write -
# execution stays behind the capability registry and human approval.
_MINED_SKILL_STEP_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "apply_markdown": [
        {
            "key": "apply_markdown",
            "capability": "apply_markdown",
            "params": {},
            "compensation": {"undo": "restore_catalog_price"},
        }
    ],
    "reorder": [
        {
            "key": "reorder",
            "capability": "reorder",
            "params": {},
            "compensation": {"undo": "cancel_pending_purchase_order"},
        }
    ],
    "quarantine_stock": [
        {
            "key": "quarantine_stock",
            "capability": "quarantine_stock",
            "params": {},
            "compensation": {"undo": "release_quarantine_hold"},
        }
    ],
    "dispatch_facilities_check": [
        {
            "key": "dispatch_facilities_check",
            "capability": "dispatch_facilities_check",
            "params": {},
            "compensation": {"undo": "cancel_facilities_dispatch"},
        }
    ],
}


def _mined_skill_drafts(tenant_id: str, data_domain: str) -> list[Any]:
    """Mine playbook drafts from this tenant's REAL resolved-outcome history.

    Trigger is the decision's scenario id - the same stable workload classification the
    rest of the platform uses - so a draft reads as "apply_markdown when
    stage4_loadshedding_x_payday_yoghurt" and its evidence refs point at the actual
    decisions that earned it.
    """
    decisions = {
        str(decision.get("id")): decision
        for decision in decision_store.list()
        if decision_tenant_id(decision, tenant_id) == tenant_id
    }
    stats = SkillStats()
    for record in _learning_outcome_records(tenant_id, data_domain):
        decision = decisions.get(record.evidence_refs[0]) if record.evidence_refs else None
        scenario_id = str((decision or {}).get("scenario_id") or "") or record.action
        stats.reflect(record, trigger=scenario_id)
    return draft_skills(stats, tenant_id=tenant_id, step_template=_MINED_SKILL_STEP_TEMPLATES)


@router.get("/mlops/skills/mined")
def list_mined_skills(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Playbooks mined from repeated, measurably successful outcomes - drafts for review."""
    resolved_domain = data_domain or _chat_data_domain()
    drafts = _mined_skill_drafts(ctx.tenant_id, resolved_domain)
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": resolved_domain,
        "skills": [skill.to_dict() for skill in drafts],
    }


@router.post(
    "/mlops/skills/mined/{skill_id}/activate",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def activate_mined_skill(
    skill_id: str,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Activate a reviewed mined draft and compile it to the validated plan shape.

    The compiled plan is returned as a governed artifact for the approving human - it is
    NOT executed here. Activation re-mines from current outcome history, so a draft that
    later outcomes no longer support simply no longer exists to activate (the honest
    tombstone: evidence, not memory of past drafts, decides what is activatable).
    """
    resolved_domain = data_domain or _chat_data_domain()
    draft = next(
        (
            skill
            for skill in _mined_skill_drafts(ctx.tenant_id, resolved_domain)
            if skill.id == skill_id
        ),
        None,
    )
    if draft is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No currently-minable draft has that id - either it never existed or "
                "later outcomes no longer support it"
            ),
        )
    active = activate_skill(draft)
    plan = skill_to_plan(
        active, plan_id=f"plan_{skill_id}_{ctx.tenant_id}", actor_role=ctx.role.value
    )
    return {"skill": active.to_dict(), "plan": plan}


@router.get("/mlops/skills")
def list_skill_manifests(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    """List the assistant skill catalogue this tenant can discover, with lifecycle status."""
    manifests = skill_registry.list(tenant_id=ctx.tenant_id)
    return {
        "tenant_id": ctx.tenant_id,
        "skills": [manifest.to_dict() for manifest in manifests],
    }


class SkillPromotionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(min_length=1, max_length=200)


@router.post(
    "/mlops/skills/{skill_id}/promote",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def promote_skill(
    skill_id: str,
    body: SkillPromotionBody,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Flip a draft skill to promoted - only past its own evaluation bar.

    The promotion gate is the enforcement point that makes the lifecycle real: discovery
    only ever surfaces promoted manifests, so this route is how a validated draft skill
    actually reaches conversations. Requires an approval-capable role, like every other
    governance write.
    """
    try:
        manifest = promote_skill_manifest(
            skill_registry,
            skill_id,
            evaluation_registry=evaluation_registry,
            evaluation_id=body.evaluation_id,
            tenant_id=ctx.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": manifest.to_dict()}


@router.post(
    "/mlops/skills/{skill_id}/retire",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def retire_skill(
    skill_id: str,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Retire a skill from discovery permanently (re-register a new version to revive)."""
    try:
        manifest = retire_skill_manifest(skill_registry, skill_id, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": manifest.to_dict()}


@router.post(
    "/mlops/plans/execute",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
async def execute_plan(
    body: dict[str, Any],
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Execute a validated governed plan through the journaled runner.

    The governed-write phase, live: the only registered write capability is the HITL
    write-back task sink, every step is journaled with compensation recorded, and the
    plan's tenant is forced to the caller's - a plan can never execute across tenants.
    Compile plans via /mlops/skills/mined/{id}/activate; execute them here after review.
    """
    try:
        plan = Plan.model_validate({**body, "tenant_id": ctx.tenant_id})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:400]) from exc
    result = await plan_runner.run(plan)
    if result.status != "done":
        raise HTTPException(
            status_code=422,
            detail=f"plan failed at step: {result.failed_step}",
        )
    return {"result": result.to_dict()}
