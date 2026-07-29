from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from time import perf_counter
from typing import Any

from shelfwise_contracts import (
    AgentName,
    Decision,
    DecisionStatus,
    Event,
    EventType,
    EvidenceObject,
    Money,
    RecommendedAction,
    RiskTier,
    SourceRef,
    TraceSpan,
    new_id,
)
from shelfwise_decision_science import (
    InventoryPolicyInput,
    Relation,
    RelationStore,
    SupplierProfile,
    compute_reorder_policy,
    detect_robust_anomaly,
    forecast_demand,
    recommend_suppliers,
    score_cold_chain_risk,
    score_expiry_risk,
    simulate_markdown,
)
from shelfwise_inference import load_inference_config
from shelfwise_memory import routed_metric

from .product_policies import resolve_product_policy
from .tenant import default_tenant_context
from .world_facts import WorldFactsProvider
from .world_facts import default_facts_provider as _default_facts


def _event_tenant_id(event: Event | None) -> str:
    return event.tenant_id if event is not None else default_tenant_context().tenant_id

# Scenario slugs classify workloads; they are not decision identities. Event-driven decisions use
# the immutable event id so retries remain idempotent, while explicit demo runs mint a new decision.
_GOLDEN_SCENARIO_ID = "stage4_loadshedding_x_payday_yoghurt"
_PROCUREMENT_SCENARIO_ID = "procurement_reorder_supplier_cover"
_SALES_SCENARIO_ID = "pos_sale_price_integrity"
_COLD_CHAIN_SCENARIO_ID = "cold_chain_generator_failure_facilities_review"

# Story physics for the two simulation scenarios above, applied ONLY when a
# world_simulation event does not carry its own telemetry (scenario drills replay the
# named story: a 3h loadshedding outage on payday, and a generator failure with a
# measured 4h outage). Live operational events can never inherit these - the cascade
# dispatcher fails closed on missing telemetry before this code runs
# (_require_operational_context in cascade_dispatcher.py), so invented physics cannot
# reach a real store's decisions.
_GOLDEN_STORY_COLD_AREA = "fridge_a"
_GOLDEN_STORY_OUTAGE_HOURS = "3"
_GOLDEN_STORY_AVERAGE_TEMP_C = "7"
_GOLDEN_STORY_PAYDAY_MULTIPLIER = "1.35"
_COLD_CHAIN_STORY_DIAGNOSIS = "generator_failed"
_COLD_CHAIN_STORY_SEVERITY = 2
_COLD_CHAIN_STORY_MINUTES_TO_UNSAFE = Decimal("18")
_COLD_CHAIN_STORY_OUTAGE_HOURS = Decimal("4")
_COLD_CHAIN_STORY_AVERAGE_TEMP_C = Decimal("8.2")
_RECALL_SCENARIO_ID = "supplier_lot_recall_quarantine"
_INVENTORY_EXCEPTION_SCENARIO_ID = "inventory_exception_review"
_INVENTORY_EXCEPTION_TYPES = frozenset({"return", "damage", "shrink", "misplaced_stock"})
_CRITIC_REJECTION_SCENARIO_ID = "critic_rejects_unsupported_supplier_switch"


def _supporting_fact(fact: str, value: object, source: str, method: str) -> dict[str, Any]:
    return {"fact": fact, "value": str(value), "source": source, "method": method}


def _span(name: str, start: float, detail: dict[str, Any] | None = None) -> TraceSpan:
    elapsed_ms = int((perf_counter() - start) * 1000)
    return TraceSpan(name=name, status="ok", ms=elapsed_ms, detail=detail or {})


def _monitor_action(sku: str) -> RecommendedAction:
    """The low-risk "hold, no action needed" verdict shared by every scenario's routing.

    Both the deterministic cascades below and the agentic tracks in agentic_cascade.py
    construct this identical fallback action - shared here instead of five independent
    literal copies.
    """
    return RecommendedAction("monitor", {"sku": sku}, RiskTier.LOW)


def _learning_data_domain(event: Event | None) -> str:
    """Match `shelfwise_memory`'s own default exactly, or a lookup would hit the wrong key.

    The learning store falls back to "world_simulation" for any decision missing a
    data_domain (manual demo runs never go through `cascade_dispatcher`, which is the
    only place that stamps one); this mirrors that default rather than inventing a
    second one that could silently drift from it.
    """
    return event.data_domain.value if event is not None else "world_simulation"


def _learned_threshold_evidence(
    *,
    learning: Any | None,
    action: RecommendedAction,
    tenant_id: str,
    data_domain: str,
) -> EvidenceObject | None:
    """Surface this decision's own learned high-water mark as evidence, never as a gate.

    `learning_store.thresholds()` tracks the largest measured value ever proven for this
    exact SKU/metric (`shelfwise_memory.routed_metric`) - a monotonic high-water mark
    recorded for the `/mlops` economics dashboard's "visible learning moment," not a
    live control parameter. Gating a critic on "biggest profit this SKU ever proved"
    would reject perfectly good smaller markdowns whenever one past decision happened
    to be unusually large - the wrong shape for a pass/fail bar. It is exactly the
    right shape for evidence: closing the loop by giving the human reviewer the
    system's own memory ("this SKU has recovered up to R X before") without touching
    critic_passed, decision status, or the routed action at all. Returns None (adds
    nothing) when no prior threshold exists yet, so a SKU's first-ever decision is
    unaffected - this is purely additive, never required.
    """
    if learning is None:
        return None
    metric, subject = routed_metric({"action": action.to_dict()})
    previous = learning.thresholds(tenant_id=tenant_id, data_domain=data_domain).get(metric)
    if not previous:
        return None
    return EvidenceObject(
        agent=AgentName.OPPORTUNITY,
        conclusion=(
            f"{subject} has previously proven {previous} measured minor units for this "
            "metric - historical context for the reviewer, not a pass/fail bar."
        ),
        supporting_data=[
            _supporting_fact(
                "previous_high_water_mark_minor_units", previous, "learning_memory", metric
            )
        ],
        confidence=Decimal("0.70"),
        recommended_action=action,
        sources=(SourceRef.tool("get_thresholds"),),
        requires_human_review=False,
    )


def _facts_source_dataset(facts: object) -> str:
    """Name the measured source without assuming every provider is the demo world."""
    return str(getattr(facts, "source_dataset", "generated_world"))


def _facts_source_method(facts: object) -> str:
    """Describe how source facts entered the cascade evidence."""
    return str(getattr(facts, "source_method", "generated_world_projection"))


def _event_source_dataset(event: Event, simulation_source: str) -> str:
    """Use the live twin label for operational events and demo labels only in simulation."""
    return (
        "operational_twin"
        if event.data_domain.value == "operational_twin"
        else simulation_source
    )


def _enforce_critic_verdict(
    *,
    critic_passed: bool,
    executive_action: RecommendedAction,
    safe_action: RecommendedAction,
) -> tuple[RecommendedAction, bool]:
    """Make the critic's verdict binding on the executive's routing, not advisory.

    This is the single authoritative home of the Critic->Executive contract for both
    cascade layers. In the agentic layer the critic's verdict reaches the executive
    only as prose inside a prompt, and prose is not enforcement: a hallucinating
    executive can answer with the escalating action even though the critic failed the
    work. In the deterministic layer the same contract holds by construction only if
    every builder routes evidence, decision action, and decision status through this
    one gate instead of re-deriving `x if critic_passed else y` per call site - the
    golden builder shipped the escalating action on a failed verdict for exactly that
    reason. A failed critic verdict always routes the safe action, and choosing the
    safe action is always allowed regardless of the critic (an executive may be more
    conservative than the critic, never less).

    Returns (final_action, override_applied) so callers can put the override on the
    decision record for auditability instead of hiding the disagreement.
    """
    if critic_passed or executive_action.type == safe_action.type:
        return executive_action, False
    return safe_action, True


def _critic_gate_receipt(
    *,
    critic_passed: bool,
    executive_action_type: str,
    override_applied: bool,
) -> dict[str, Any]:
    """One auditable record of what each agent said and what the gate did about it."""
    return {
        "critic_passed": critic_passed,
        "executive_action_type": executive_action_type,
        "override_applied": override_applied,
    }


def run_golden_cascade(
    event: Event | None = None,
    *,
    facts: WorldFactsProvider | None = None,
    learning: Any | None = None,
) -> dict[str, Any]:
    """Run the golden expiry-markdown scenario against the generated world.

    This is the first real vertical slice: deterministic math produces facts; product agents wrap
    those facts into evidence; the critic checks them; the executive emits one pending HITL action.
    """
    correlation_id = event.correlation_id if event is not None else new_id("cor")
    resolved_facts = facts or _default_facts()
    scenario = resolved_facts.get_scenario_facts(_event_tenant_id(event))
    sku = scenario.sku
    product = scenario.product_name
    source_dataset = _facts_source_dataset(resolved_facts)
    source_method = _facts_source_method(resolved_facts)
    source_stock = SourceRef.dataset(source_dataset, f"stock:sku:{sku}")
    source_sales = SourceRef.dataset(source_dataset, f"sales:sku:{sku}")
    payload = event.payload if event is not None else {}
    cold_area = str(payload.get("cold_chain_area") or _GOLDEN_STORY_COLD_AREA)
    outage_hours = Decimal(
        str(payload.get("cold_chain_outage_hours") or _GOLDEN_STORY_OUTAGE_HOURS)
    )
    average_temp_c = Decimal(
        str(payload.get("cold_chain_average_temp_c") or _GOLDEN_STORY_AVERAGE_TEMP_C)
    )
    payday_multiplier = Decimal(
        str(payload.get("payday_multiplier") or _GOLDEN_STORY_PAYDAY_MULTIPLIER)
    )
    source_outage = SourceRef.dataset(source_dataset, f"{scenario.location}:{cold_area}")
    spans: list[TraceSpan] = []
    evidence: list[EvidenceObject] = []
    inference = load_inference_config()

    started = perf_counter()
    demand = forecast_demand(
        sku=sku,
        recent_daily_units=list(scenario.recent_daily_units),
        horizon_days=3,
        payday_multiplier=payday_multiplier,
    )
    spans.append(
        _span(
            "decision_science.forecast_demand",
            started,
            {"daily_units": str(demand.daily_units)},
        )
    )

    started = perf_counter()
    cold = score_cold_chain_risk(
        area=cold_area,
        outage_hours=outage_hours,
        average_temp_c=average_temp_c,
    )
    spans.append(_span("decision_science.score_cold_chain_risk", started, {"risk": str(cold.risk)}))

    started = perf_counter()
    expiry = score_expiry_risk(
        sku=sku,
        units_on_hand=Decimal(scenario.units_on_hand),
        days_to_expiry=Decimal(scenario.days_to_expiry),
        forecast_daily_units=demand.daily_units,
        unit_cost=scenario.unit_cost,
        cold_chain_risk=cold.risk,
        cold_chain_penalty_days=cold.penalty_days,
    )
    spans.append(_span("decision_science.score_expiry_risk", started, {"risk": str(expiry.risk)}))

    started = perf_counter()
    # The candidate markdown is a per-family business rule owned by the product-policy
    # layer, never an inline literal in decision logic.
    product_policy = resolve_product_policy(scenario.category)
    markdown_discount = Decimal(product_policy.markdown_discount_pct)
    simulation = simulate_markdown(
        sku=sku,
        units_on_hand=Decimal(scenario.units_on_hand),
        days_to_expiry=expiry.effective_days_to_expiry,
        base_daily_units=demand.daily_units,
        unit_price=scenario.unit_price,
        unit_cost=scenario.unit_cost,
        discount_pct=markdown_discount,
    )
    markdown_margin = (
        scenario.unit_price * (Decimal("1") - markdown_discount)
    ) - scenario.unit_cost
    spans.append(
        _span(
            "decision_science.simulate_markdown",
            started,
            {"incremental_profit": str(simulation.incremental_profit)},
        )
    )

    monitor = _monitor_action(sku)
    markdown = RecommendedAction(
        "apply_markdown",
        {
            "sku": sku,
            "discount_pct": product_policy.markdown_discount_pct,
            "duration_hours": product_policy.markdown_duration_hours,
        },
        RiskTier.HIGH,
    )

    evidence.append(
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"{product} has {scenario.units_on_hand} units on hand at {scenario.location}."
            ),
            supporting_data=[
                _supporting_fact(
                    "units_on_hand",
                    scenario.units_on_hand,
                    str(source_stock),
                    source_method,
                )
            ],
            confidence=Decimal("0.92"),
            recommended_action=monitor,
            sources=(source_stock,),
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.DEMAND,
            conclusion=f"Payday-adjusted demand is {demand.daily_units} units/day.",
            supporting_data=[
                _supporting_fact(
                    "forecast_daily_units",
                    demand.daily_units,
                    str(source_sales),
                    demand.method,
                )
            ],
            confidence=demand.confidence,
            recommended_action=monitor,
            sources=(source_sales,),
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXPIRY,
            conclusion=f"Cold-chain pressure leaves {expiry.waste_units} units at risk of waste.",
            supporting_data=[
                _supporting_fact("expiry_risk", expiry.risk, str(source_outage), expiry.method),
                _supporting_fact(
                    "zar_at_risk",
                    expiry.zar_at_risk,
                    str(source_stock),
                    "unit_cost_x_waste",
                ),
            ],
            confidence=expiry.confidence,
            recommended_action=markdown,
            sources=(source_stock, source_outage),
            requires_human_review=True,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.OPPORTUNITY,
            conclusion=(
                "A 20% markdown recovers more value than holding stock through the outage window."
            ),
            supporting_data=[
                _supporting_fact(
                    "incremental_profit",
                    simulation.incremental_profit,
                    "simulate_markdown",
                    simulation.method,
                )
            ],
            confidence=Decimal("0.82"),
            recommended_action=markdown,
            sources=(SourceRef.tool("simulate_markdown"),),
            requires_human_review=True,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.SIMULATION,
            conclusion=(
                f"Markdown sells {simulation.markdown_units_sold} units and cuts waste from "
                f"{simulation.hold_waste_units} to {simulation.markdown_waste_units} units."
            ),
            supporting_data=[
                _supporting_fact(
                    "hold_waste_units",
                    simulation.hold_waste_units,
                    "simulate_markdown",
                    simulation.method,
                ),
                _supporting_fact(
                    "markdown_waste_units",
                    simulation.markdown_waste_units,
                    "simulate_markdown",
                    simulation.method,
                ),
            ],
            confidence=simulation.confidence,
            recommended_action=markdown,
            sources=(SourceRef.tool("simulate_markdown"),),
            requires_human_review=True,
        )
    )
    history_evidence = _learned_threshold_evidence(
        learning=learning,
        action=markdown,
        tenant_id=_event_tenant_id(event),
        data_domain=_learning_data_domain(event),
    )
    if history_evidence is not None:
        evidence.append(history_evidence)

    critic_passed = simulation.incremental_profit.cents > 0 and all(
        item.sources for item in evidence
    )
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=markdown,
        safe_action=monitor,
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Recommendation passes: it is sourced, math-backed, and requires HITL approval."
                if critic_passed
                else "Markdown does not recover measured value; monitor instead of discounting."
            ),
            supporting_data=[
                _supporting_fact(
                    "critic_passed",
                    critic_passed,
                    "critic_gate",
                    "source_and_value_check",
                )
            ],
            confidence=Decimal("0.88"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("critic_gate"),),
            requires_human_review=critic_passed,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Approve a 20% markdown for SKU {sku} now, then review outcome after 24 hours."
                if critic_passed
                else f"Hold the markdown for SKU {sku}; monitor until value at risk is measured."
            ),
            supporting_data=[
                _supporting_fact(
                    "priority",
                    "single_action",
                    "executive_policy",
                    "risk_adjusted_expected_value",
                )
            ],
            confidence=Decimal("0.86"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("executive_policy"), SourceRef.tool("critic_gate")),
            requires_human_review=critic_passed,
        )
    )

    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING if critic_passed else DecisionStatus.REJECTED,
        action=routed_action,
        caused_by=(_cause_id(event, correlation_id),),
        summary=(
            f"Pending manager approval: 20% markdown for {product} at {scenario.location}."
            if critic_passed
            else f"Critic rejected the markdown for {product}; monitoring {scenario.location}."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _GOLDEN_SCENARIO_ID
    decision_payload["role"] = "store_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=markdown.type,
        override_applied=gate_override,
    )
    decision_payload["expected_outcome"] = {
        "predicted_sell_through_units": _whole_units(simulation.markdown_units_sold),
        "predicted_waste_units": _whole_units(simulation.markdown_waste_units),
        "incremental_profit_minor_units": simulation.incremental_profit.minor_units,
        "incremental_profit": simulation.incremental_profit.to_dict(),
        "markdown_margin_minor_units": markdown_margin.minor_units,
    }

    return {
        "correlation_id": correlation_id,
        "scenario": _GOLDEN_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict() for span in spans],
        "inference": inference.to_public_dict(),
        "seed_data": scenario.to_dict(),
        "store_intelligence": resolved_facts.get_store_intelligence(_event_tenant_id(event)),
        "learning": {
            "status": "armed",
            "message": "After approval, compare actual sell-through with simulated sell-through.",
        },
    }


def run_procurement_cascade(
    event: Event | None = None,
    *,
    facts: WorldFactsProvider | None = None,
    learning: Any | None = None,
) -> dict[str, Any]:
    """Run the procurement role path: reorder policy plus measured supplier choice."""

    correlation_id = event.correlation_id if event is not None else new_id("cor")
    resolved_facts = facts or _default_facts()
    scenario = resolved_facts.get_scenario_facts(_event_tenant_id(event))
    sku = scenario.sku
    product = scenario.product_name
    source_dataset = _facts_source_dataset(resolved_facts)
    source_method = _facts_source_method(resolved_facts)
    source_stock = SourceRef.dataset(source_dataset, f"stock:sku:{sku}")
    source_sales = SourceRef.dataset(source_dataset, f"sales:sku:{sku}")
    source_suppliers = SourceRef.dataset(source_dataset, "suppliers")
    spans: list[TraceSpan] = []
    evidence: list[EvidenceObject] = []

    started = perf_counter()
    recent = tuple(Decimal(value) for value in scenario.recent_daily_units)
    avg_daily_demand = sum(recent) / Decimal(len(recent)) if recent else Decimal("1")
    variance = (
        sum((value - avg_daily_demand) ** 2 for value in recent) / Decimal(len(recent))
        if recent
        else Decimal("0")
    )
    demand_std = variance.sqrt() if variance > 0 else Decimal("0")
    policy = compute_reorder_policy(
        InventoryPolicyInput(
            sku=sku,
            on_hand=Decimal(scenario.units_on_hand),
            committed_units=Decimal("0"),
            avg_daily_demand=avg_daily_demand,
            demand_std=demand_std,
            lead_time_days=scenario.supplier_lead_time_days,
            unit_cost=scenario.unit_cost,
        )
    )
    spans.append(
        _span(
            "decision_science.compute_reorder_policy",
            started,
            {
                "reorder_point_units": str(policy.reorder_point_units),
                "stockout_risk": str(policy.stockout_risk),
            },
        )
    )

    graph = RelationStore()
    current = resolved_facts.get_supplier_for_sku(_event_tenant_id(event), sku)
    alternate = resolved_facts.get_alternate_supplier(
        _event_tenant_id(event), exclude=current["supplier_id"]
    )
    current_supplier = str(current["supplier_id"])
    graph.add(Relation(f"sku:{sku}", "supplied_by", current_supplier))
    profiles = {
        current_supplier: SupplierProfile(
            supplier_id=current_supplier,
            lead_time_days=Decimal(str(current["lead_time_days"])),
            fill_rate=Decimal(str(current["fill_rate"])),
            unit_cost=scenario.unit_cost,
        ),
    }
    if alternate is not None:
        backup_supplier = str(alternate["supplier_id"])
        graph.add(Relation(f"sku:{sku}", "supplied_by", backup_supplier))
        profiles[backup_supplier] = SupplierProfile(
            supplier_id=backup_supplier,
            lead_time_days=Decimal(str(alternate["lead_time_days"])),
            fill_rate=Decimal(str(alternate["fill_rate"])),
            unit_cost=scenario.unit_cost,
        )

    started = perf_counter()
    ranking = recommend_suppliers(sku, graph, profiles)
    top_supplier = ranking.ranked[0]
    spans.append(
        _span(
            "decision_science.recommend_suppliers",
            started,
            {"top_supplier": top_supplier.supplier_id, "coverage": str(ranking.coverage)},
        )
    )

    reorder = RecommendedAction(
        "reorder",
        {
            "sku": sku,
            "supplier_id": top_supplier.supplier_id,
            "quantity_units": str(policy.suggested_order_units),
            "reorder_point_units": str(policy.reorder_point_units),
            "stockout_risk": str(policy.stockout_risk),
        },
        RiskTier.MEDIUM,
    )
    monitor = _monitor_action(sku)

    evidence.append(
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"{product} has only {policy.available_to_sell_units} sellable units "
                f"against a {policy.reorder_point_units} unit reorder point."
            ),
            supporting_data=[
                _supporting_fact(
                    "available_to_sell_units",
                    policy.available_to_sell_units,
                    str(source_stock),
                    policy.method,
                ),
                _supporting_fact(
                    "stockout_risk",
                    policy.stockout_risk,
                    str(source_sales),
                    policy.method,
                ),
                _supporting_fact(
                    "suggested_order_units",
                    policy.suggested_order_units,
                    "compute_reorder_policy",
                    policy.method,
                ),
            ],
            confidence=Decimal("0.87"),
            recommended_action=reorder if policy.should_reorder else monitor,
            sources=(source_stock, source_sales, SourceRef.tool("compute_reorder_policy")),
            requires_human_review=policy.should_reorder,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.PROCUREMENT,
            conclusion=(
                f"{top_supplier.supplier_id} is the preferred cover supplier based on "
                f"fill rate, lead time, and unit cost; coverage is {ranking.coverage}."
            ),
            supporting_data=[
                _supporting_fact(
                    "top_supplier",
                    top_supplier.supplier_id,
                    str(source_suppliers),
                    ranking.method,
                ),
                _supporting_fact(
                    "supplier_coverage",
                    ranking.coverage,
                    str(source_suppliers),
                    ranking.method,
                ),
                _supporting_fact(
                    "profiled_supplier_count",
                    len(profiles),
                    str(source_suppliers),
                    source_method,
                ),
            ],
            confidence=ranking.coverage,
            recommended_action=reorder,
            sources=(source_suppliers, SourceRef.tool("recommend_suppliers")),
            requires_human_review=True,
        )
    )
    history_evidence = _learned_threshold_evidence(
        learning=learning,
        action=reorder,
        tenant_id=_event_tenant_id(event),
        data_domain=_learning_data_domain(event),
    )
    if history_evidence is not None:
        evidence.append(history_evidence)

    critic_passed = policy.should_reorder and ranking.coverage >= Decimal("0.60")
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=reorder,
        safe_action=monitor,
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Procurement recommendation passes: quantity is reorder-policy backed "
                "and supplier choice uses measured profiles."
            ),
            supporting_data=[
                _supporting_fact(
                    "critic_passed",
                    critic_passed,
                    "critic_gate",
                    "reorder_policy_and_supplier_profile_check",
                )
            ],
            confidence=Decimal("0.89"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("critic_gate"),),
            requires_human_review=critic_passed,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Route a replenishment request for SKU {sku} to procurement, "
                "with manager approval before any write-back."
                if critic_passed
                else f"Hold the reorder for SKU {sku}; supplier coverage or reorder policy "
                "did not clear the critic's bar."
            ),
            supporting_data=[
                _supporting_fact(
                    "priority",
                    "stockout_prevention",
                    "executive_policy",
                    "risk_adjusted_service_level",
                )
            ],
            confidence=Decimal("0.85"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("executive_policy"), SourceRef.tool("critic_gate")),
            requires_human_review=critic_passed,
        )
    )

    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING if critic_passed else DecisionStatus.REJECTED,
        action=routed_action,
        caused_by=(_cause_id(event, correlation_id),),
        summary=(
            f"Pending procurement approval: reorder {product} from {top_supplier.supplier_id}."
            if critic_passed
            else f"Critic rejected the reorder for {product}; "
            f"monitoring {top_supplier.supplier_id}."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _PROCUREMENT_SCENARIO_ID
    decision_payload["role"] = "procurement_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=reorder.type,
        override_applied=gate_override,
    )
    decision_payload["expected_outcome"] = {
        "suggested_order_units": str(policy.suggested_order_units),
        "stockout_risk": str(policy.stockout_risk),
        "supplier_id": top_supplier.supplier_id,
        "supplier_coverage": str(ranking.coverage),
        "stockout_exposure": policy.zar_exposure.to_dict(),
        "stockout_exposure_minor_units": policy.zar_exposure.minor_units,
        # `/mlops`'s decision-economics dashboard (`_attach_decision_governance`) reads only
        # `incremental_profit_minor_units` - without it every approved reorder displayed
        # "R0.00 recovered" despite the real, computed stockout exposure above.
        "incremental_profit_minor_units": policy.zar_exposure.minor_units,
    }

    return {
        "correlation_id": correlation_id,
        "scenario": _PROCUREMENT_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict() for span in spans],
        "inference": load_inference_config().to_public_dict(),
        "seed_data": scenario.to_dict(),
        "supplier_ranking": ranking.to_dict(),
        "reorder_policy": policy.to_dict(),
        "learning": {
            "status": "armed",
            "message": "After approval, compare supplier fill rate and stockout avoidance.",
        },
    }


def run_sales_cascade(
    event: Event | None = None, *, facts: WorldFactsProvider | None = None
) -> dict[str, Any]:
    """Run the Sales/POS role path over a sale event and catalogue price reference."""

    correlation_id = event.correlation_id if event is not None else new_id("cor")
    resolved_facts = facts or _default_facts()
    scenario = resolved_facts.get_scenario_facts(_event_tenant_id(event))
    sku = _payload_value(event, "sku", scenario.sku)
    location = _payload_value(event, "location", scenario.location)
    quantity = Decimal(str(_payload_value(event, "quantity", scenario.recent_daily_units[-1])))
    unit_price = Decimal(str(_payload_value(event, "unit_price", scenario.unit_price.amount)))
    expected_price = scenario.unit_price.amount
    line_revenue = Money.zar(unit_price * quantity)
    expected_revenue = scenario.unit_price * quantity
    price_delta = unit_price - expected_price
    source_dataset = _facts_source_dataset(resolved_facts)
    source_pos = SourceRef.dataset(source_dataset, f"sales:sku:{sku}")
    source_product = SourceRef.dataset(source_dataset, f"products:sku:{sku}")
    spans: list[TraceSpan] = []

    started = perf_counter()
    anomaly = detect_robust_anomaly(
        metric_name="pos_sale_units",
        current_value=quantity,
        history=list(scenario.recent_daily_units),
    )
    spans.append(
        _span(
            "decision_science.detect_pos_velocity_anomaly",
            started,
            {"score": str(anomaly.score), "is_anomaly": anomaly.is_anomaly},
        )
    )

    price_matches = price_delta == Decimal("0")
    action = (
        RecommendedAction(
            "record_sale",
            {
                "sku": sku,
                "location": location,
                "quantity": str(quantity),
                "unit_price": str(unit_price),
            },
            RiskTier.LOW,
        )
        if price_matches
        else RecommendedAction(
            "review_price_exception",
            {
                "sku": sku,
                "location": location,
                "observed_unit_price": str(unit_price),
                "catalog_unit_price": str(expected_price),
                "quantity": str(quantity),
            },
            RiskTier.MEDIUM,
        )
    )
    status = DecisionStatus.APPROVED if price_matches else DecisionStatus.PENDING

    evidence = [
        EvidenceObject(
            agent=AgentName.SALES,
            conclusion=(
                f"POS recorded {quantity} units of SKU {sku} at R{unit_price} "
                f"against catalogue price R{expected_price}."
            ),
            supporting_data=[
                _supporting_fact("sale_quantity", quantity, str(source_pos), "pos_csv_event"),
                _supporting_fact(
                    "line_revenue",
                    line_revenue,
                    str(source_pos),
                    "quantity_x_unit_price",
                ),
                _supporting_fact(
                    "price_delta",
                    price_delta,
                    str(source_product),
                    "catalog_price_integrity",
                ),
            ],
            confidence=Decimal("0.91") if price_matches else Decimal("0.72"),
            recommended_action=action,
            sources=(source_pos, source_product),
            requires_human_review=not price_matches,
        ),
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Sale can be recorded automatically."
                if price_matches
                else "Sale price differs from catalogue; route to manager before write-back."
            ),
            supporting_data=[
                _supporting_fact(
                    "price_matches_catalog",
                    price_matches,
                    "critic_gate",
                    "catalog_price_check",
                ),
                _supporting_fact(
                    "velocity_anomaly",
                    anomaly.is_anomaly,
                    "detect_robust_anomaly",
                    anomaly.method,
                ),
            ],
            confidence=Decimal("0.88"),
            recommended_action=action,
            sources=(SourceRef.tool("critic_gate"), SourceRef.tool("detect_robust_anomaly")),
            requires_human_review=not price_matches,
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                "No intervention needed; keep the POS sale as the demand signal."
                if price_matches
                else "Review the price exception before allowing downstream replenishment signals."
            ),
            supporting_data=[
                _supporting_fact(
                    "priority",
                    "price_integrity",
                    "executive_policy",
                    "pos_signal_quality",
                )
            ],
            confidence=Decimal("0.84"),
            recommended_action=action,
            sources=(SourceRef.tool("executive_policy"),),
            requires_human_review=not price_matches,
        ),
    ]

    decision = Decision(
        id=_decision_id(event),
        status=status,
        action=action,
        caused_by=(_cause_id(event, correlation_id),),
        summary=(
            f"POS sale for SKU {sku} recorded cleanly."
            if price_matches
            else f"Pending price exception review for SKU {sku}."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _SALES_SCENARIO_ID
    decision_payload["role"] = "sales_manager"
    decision_payload["critic_verdict"] = "approved" if price_matches else "review_required"
    decision_payload["expected_outcome"] = {
        "line_revenue": line_revenue.to_dict(),
        "line_revenue_minor_units": line_revenue.minor_units,
        "expected_revenue": expected_revenue.to_dict(),
        "expected_revenue_minor_units": expected_revenue.minor_units,
        "price_delta": str(price_delta),
        "velocity_anomaly": anomaly.to_dict(),
    }

    return {
        "correlation_id": correlation_id,
        "scenario": _SALES_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict() for span in spans],
        "inference": load_inference_config().to_public_dict(),
        "seed_data": scenario.to_dict(),
        "learning": {
            "status": "captured",
            "message": "POS sale is available as a demand and price-integrity signal.",
        },
    }


# Normal till variance (promotions, rounding, shelf-band pricing) sits within +/-10% of the
# catalogue price; anything past this band is a genuine exception worth a manager's attention.
# A store selling thousands of SKUs cannot page a human for routine variance, so in-band sales
# deliberately return None (recorded as demand signals, no decision minted).
PRICE_EXCEPTION_TOLERANCE = Decimal("0.15")
_PRICE_OUTLIER_SCENARIO_ID = "pos_price_outlier_review"


def run_catalog_price_check(event: Event) -> dict[str, Any] | None:
    """Check any SKU's observed till price against its catalogue price.

    This is the whole-store generalization of run_sales_cascade's price-integrity idea:
    it works for every product that carries both an observed and a catalogue price, not
    just the seeded hero SKU. Only genuine outliers produce a pending HITL decision.
    """
    payload = event.payload
    try:
        unit_price_c = int(payload["unit_price_cents"])
        catalog_price_c = int(payload["catalog_price_cents"])
    except (KeyError, TypeError, ValueError):
        return None
    if catalog_price_c <= 0 or unit_price_c < 0:
        return None

    started = perf_counter()
    delta_pct = (Decimal(unit_price_c) - Decimal(catalog_price_c)) / Decimal(catalog_price_c)
    span = _span(
        "decision_science.check_price_band",
        started,
        {"delta_pct": str(delta_pct), "tolerance": str(PRICE_EXCEPTION_TOLERANCE)},
    )
    if abs(delta_pct) <= PRICE_EXCEPTION_TOLERANCE:
        return None

    correlation_id = event.correlation_id
    sku = str(payload.get("sku", "unknown"))
    units = _int_payload(payload, "units", 1)
    observed = Money(minor_units=unit_price_c)
    catalog = Money(minor_units=catalog_price_c)
    exposure = Money(minor_units=(unit_price_c - catalog_price_c) * max(units, 1))
    delta_display = f"{(delta_pct * 100).quantize(Decimal('0.1'))}%"
    source_pos = SourceRef.dataset(_event_source_dataset(event, "worldgen_pos"), f"sale:{event.id}")
    source_catalog = SourceRef.dataset(
        _event_source_dataset(event, "worldgen_catalog"), f"catalog:sku:{sku}"
    )

    action = RecommendedAction(
        "review_price_exception",
        {
            "sku": sku,
            "observed_unit_price": str(observed.amount),
            "catalog_unit_price": str(catalog.amount),
            "units": str(units),
            "price_delta_pct": delta_display,
        },
        RiskTier.MEDIUM,
    )
    evidence = [
        EvidenceObject(
            agent=AgentName.SALES,
            conclusion=(
                f"Till price R{observed.amount} for SKU {sku} sits {delta_display} away from "
                f"catalogue price R{catalog.amount} - outside the normal variance band."
            ),
            supporting_data=[
                _supporting_fact("observed_unit_price", observed, str(source_pos), "pos_event"),
                _supporting_fact(
                    "catalog_unit_price", catalog, str(source_catalog), "product_master"
                ),
                _supporting_fact(
                    "price_delta_pct", delta_display, str(source_catalog), "price_band_check"
                ),
            ],
            confidence=Decimal("0.78"),
            recommended_action=action,
            sources=(source_pos, source_catalog),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Price deviation exceeds the tolerated band; hold write-back until a manager "
                "confirms whether this is a mislabel, an override, or a catalogue error."
            ),
            supporting_data=[
                _supporting_fact("within_tolerance", False, "critic_gate", "price_band_check"),
                _supporting_fact(
                    "tolerance_pct",
                    f"{(PRICE_EXCEPTION_TOLERANCE * 100).quantize(Decimal('1'))}%",
                    "critic_gate",
                    "price_band_check",
                ),
            ],
            confidence=Decimal("0.86"),
            recommended_action=action,
            sources=(SourceRef.tool("critic_gate"),),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=(
            f"Pending price exception for SKU {sku}: till R{observed.amount} vs "
            f"catalogue R{catalog.amount} ({delta_display})."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _PRICE_OUTLIER_SCENARIO_ID
    decision_payload["role"] = "sales_manager"
    decision_payload["critic_verdict"] = "review_required"
    decision_payload["expected_outcome"] = {
        "revenue_exposure": exposure.to_dict(),
        "revenue_exposure_minor_units": exposure.minor_units,
        "price_delta_pct": delta_display,
    }

    return {
        "correlation_id": correlation_id,
        "scenario": _PRICE_OUTLIER_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict()],
        "inference": load_inference_config().to_public_dict(),
        "learning": {
            "status": "armed",
            "message": "After review, feed the confirmed cause back into catalogue hygiene.",
        },
    }


# Fresh stock inside this window is a real markdown call a human should confirm; anything
# with more runway is routine rotation the store handles without a decision.
EXPIRY_REVIEW_MAX_DAYS = 2
_EXPIRY_SCENARIO_ID = "expiry_risk_markdown_review"


def run_expiry_risk_check(event: Event) -> dict[str, Any] | None:
    """Turn an imminent-expiry batch into a pending inventory-manager review.

    Third decision domain alongside price integrity and cold chain, so unattended
    runs exercise more than one role and action type. Batches with comfortable
    runway return None - no decision minted, the event stays a rotation signal.
    """
    payload = event.payload
    try:
        days = int(payload["days_to_expiry"])
    except (KeyError, TypeError, ValueError):
        return None
    if days > EXPIRY_REVIEW_MAX_DAYS:
        return None

    started = perf_counter()
    sku = str(payload.get("sku", "unknown"))
    batch_id = str(payload.get("batch_id", ""))
    correlation_id = event.correlation_id
    span = _span(
        "decision_science.check_expiry_window",
        started,
        {"days_to_expiry": str(days), "max_days": str(EXPIRY_REVIEW_MAX_DAYS)},
    )
    source = SourceRef.dataset(_event_source_dataset(event, "worldgen_wms"), f"expiry:{event.id}")
    action = RecommendedAction(
        "review_expiry_markdown",
        {"sku": sku, "batch_id": batch_id, "days_to_expiry": str(days)},
        RiskTier.MEDIUM,
    )
    evidence = [
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"Batch {batch_id or sku} has {days} day(s) to expiry - inside the "
                f"{EXPIRY_REVIEW_MAX_DAYS}-day markdown review window."
            ),
            supporting_data=[
                _supporting_fact("days_to_expiry", days, str(source), "wms_expiry_entry"),
                _supporting_fact(
                    "review_window_days", EXPIRY_REVIEW_MAX_DAYS, str(source), "expiry_policy"
                ),
            ],
            confidence=Decimal("0.82"),
            recommended_action=action,
            sources=(source,),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Imminent expiry confirmed by the WMS entry; hold the markdown until an "
                "inventory manager confirms shelf state."
            ),
            supporting_data=[
                _supporting_fact("within_review_window", True, "critic_gate", "expiry_policy")
            ],
            confidence=Decimal("0.85"),
            recommended_action=action,
            sources=(SourceRef.tool("critic_gate"),),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=f"Pending expiry markdown review for SKU {sku}: {days} day(s) to expiry.",
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _EXPIRY_SCENARIO_ID
    decision_payload["role"] = "inventory_manager"
    decision_payload["critic_verdict"] = "review_required"
    decision_payload["expected_outcome"] = {"days_to_expiry": days, "batch_id": batch_id}

    return {
        "correlation_id": correlation_id,
        "scenario": _EXPIRY_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict()],
        "inference": load_inference_config().to_public_dict(),
        "learning": {
            "status": "armed",
            "message": "Confirmed markdowns feed the expiry review window threshold.",
        },
    }


def run_cold_chain_cascade(
    event: Event | None = None, *, facts: WorldFactsProvider | None = None
) -> dict[str, Any]:
    """Run the measured cold-chain alert path into facilities HITL review."""

    correlation_id = event.correlation_id if event is not None else new_id("cor")
    resolved_facts = facts or _default_facts()
    scenario = resolved_facts.get_scenario_facts(_event_tenant_id(event))
    payload = event.payload if event is not None else {}
    site_id = str(payload.get("site_id") or _payload_value(event, "location", scenario.location))
    asset_id = str(
        payload.get("asset_id") or f"cold-chain:{scenario.location}:{scenario.category}"
    )
    category = str(payload.get("category") or scenario.category)
    diagnosis = str(payload.get("diagnosis") or _COLD_CHAIN_STORY_DIAGNOSIS)
    severity = _int_payload(payload, "severity", _COLD_CHAIN_STORY_SEVERITY)
    predicted_minutes = _decimal_payload(
        payload, "predicted_minutes_to_unsafe", _COLD_CHAIN_STORY_MINUTES_TO_UNSAFE
    )
    measured_outage_hours = _decimal_payload(
        payload, "measured_outage_hours", _COLD_CHAIN_STORY_OUTAGE_HOURS
    )
    average_temp_c = _decimal_payload(payload, "temp_c", _COLD_CHAIN_STORY_AVERAGE_TEMP_C)
    stock_at_risk = _money_payload(
        payload.get("stock_at_risk"),
        default=scenario.unit_price * scenario.units_on_hand,
    )
    source_alert = SourceRef.dataset("cold_chain_alert", asset_id)
    spans: list[TraceSpan] = []
    evidence: list[EvidenceObject] = []

    started = perf_counter()
    cold = score_cold_chain_risk(
        area=asset_id,
        outage_hours=measured_outage_hours,
        average_temp_c=average_temp_c,
    )
    spans.append(
        _span(
            "decision_science.score_cold_chain_risk",
            started,
            {"risk": str(cold.risk), "penalty_days": str(cold.penalty_days)},
        )
    )

    started = perf_counter()
    # This is the cold-chain scenario, not the payday one - forecast_demand's default
    # payday_multiplier exists for the golden payday-yoghurt story and must not bleed
    # into an unrelated demand forecast here.
    demand = forecast_demand(
        sku=scenario.sku,
        recent_daily_units=list(scenario.recent_daily_units),
        horizon_days=3,
        payday_multiplier=Decimal("1"),
    )
    expiry = score_expiry_risk(
        sku=scenario.sku,
        units_on_hand=Decimal(scenario.units_on_hand),
        days_to_expiry=Decimal(scenario.days_to_expiry),
        forecast_daily_units=demand.daily_units,
        unit_cost=scenario.unit_cost,
        cold_chain_risk=cold.risk,
        cold_chain_penalty_days=cold.penalty_days,
    )
    spans.append(
        _span(
            "decision_science.score_expiry_risk",
            started,
            {"waste_units": str(expiry.waste_units), "risk": str(expiry.risk)},
        )
    )

    alert_is_actionable = severity >= 1 and stock_at_risk.minor_units > 0
    action = RecommendedAction(
        "dispatch_facilities_check" if alert_is_actionable else "monitor_cold_chain",
        {
            "site_id": site_id,
            "asset_id": asset_id,
            "category": category,
            "diagnosis": diagnosis,
            "predicted_minutes_to_unsafe": str(predicted_minutes),
            "stock_at_risk_minor_units": stock_at_risk.minor_units,
        },
        RiskTier.HIGH if severity >= 2 else RiskTier.MEDIUM,
    )
    monitor = RecommendedAction(
        "monitor_cold_chain",
        {"site_id": site_id, "asset_id": asset_id, "diagnosis": diagnosis},
        RiskTier.LOW,
    )
    routed_action = action if alert_is_actionable else monitor

    evidence.append(
        EvidenceObject(
            agent=AgentName.COLD_CHAIN,
            conclusion=(
                f"{asset_id} at {site_id} reports {diagnosis}; "
                f"{stock_at_risk} is exposed if the excursion continues."
            ),
            supporting_data=[
                _supporting_fact("severity", severity, str(source_alert), "sensor_fusion_alert"),
                _supporting_fact("diagnosis", diagnosis, str(source_alert), "sensor_fusion_alert"),
                _supporting_fact(
                    "predicted_minutes_to_unsafe",
                    predicted_minutes,
                    str(source_alert),
                    "thermal_predictor",
                ),
                _supporting_fact(
                    "stock_at_risk_minor_units",
                    stock_at_risk.minor_units,
                    str(source_alert),
                    "catalogue_x_fridge_contents",
                ),
                _supporting_fact(
                    "cold_chain_risk",
                    cold.risk,
                    "score_cold_chain_risk",
                    cold.method,
                ),
            ],
            confidence=cold.confidence,
            recommended_action=routed_action,
            sources=(source_alert, SourceRef.tool("score_cold_chain_risk")),
            requires_human_review=alert_is_actionable,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXPIRY,
            conclusion=(
                f"Measured cold-chain risk reduces effective shelf life to "
                f"{expiry.effective_days_to_expiry} days for {scenario.product_name}."
            ),
            supporting_data=[
                _supporting_fact("expiry_risk", expiry.risk, "score_expiry_risk", expiry.method),
                _supporting_fact(
                    "waste_units",
                    expiry.waste_units,
                    "score_expiry_risk",
                    expiry.method,
                ),
                _supporting_fact(
                    "zar_at_risk",
                    expiry.zar_at_risk,
                    "score_expiry_risk",
                    "unit_cost_x_waste",
                ),
            ],
            confidence=expiry.confidence,
            recommended_action=routed_action,
            sources=(SourceRef.tool("score_expiry_risk"), source_alert),
            requires_human_review=alert_is_actionable,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Cold-chain escalation passes: measured sensor alert, diagnosis, and ZAR-at-risk "
                "are present."
                if alert_is_actionable
                else "Cold-chain alert lacks enough measured impact; monitor only."
            ),
            supporting_data=[
                _supporting_fact(
                    "critic_passed",
                    alert_is_actionable,
                    "critic_gate",
                    "measured_alert_and_value_at_risk_check",
                )
            ],
            confidence=Decimal("0.90"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("critic_gate"), source_alert),
            requires_human_review=alert_is_actionable,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Route a facilities check for {asset_id} before dairy stock crosses "
                "the unsafe window."
                if alert_is_actionable
                else f"Keep monitoring {asset_id}; no manager action yet."
            ),
            supporting_data=[
                _supporting_fact(
                    "priority",
                    "prevent_spoilage",
                    "executive_policy",
                    "cold_chain_human_review",
                )
            ],
            confidence=Decimal("0.86"),
            recommended_action=routed_action,
            sources=(SourceRef.tool("executive_policy"), SourceRef.tool("critic_gate")),
            requires_human_review=alert_is_actionable,
        )
    )

    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING if alert_is_actionable else DecisionStatus.REJECTED,
        action=routed_action,
        caused_by=(_cause_id(event, correlation_id),),
        summary=(
            f"Pending facilities review for {asset_id}: {diagnosis} with {stock_at_risk} at risk."
            if alert_is_actionable
            else f"Monitor {asset_id}; no measured cold-chain intervention required."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _event_tenant_id(event)
    decision_payload["scenario_id"] = _COLD_CHAIN_SCENARIO_ID
    decision_payload["role"] = "facilities_manager"
    decision_payload["critic_verdict"] = "approved" if alert_is_actionable else "rejected"
    decision_payload["expected_outcome"] = {
        "diagnosis": diagnosis,
        "severity": severity,
        "cold_chain_risk": str(cold.risk),
        "cold_chain_penalty_days": str(cold.penalty_days),
        "effective_days_to_expiry": str(expiry.effective_days_to_expiry),
        "waste_units": str(expiry.waste_units),
        "stock_at_risk": stock_at_risk.to_dict(),
        "stock_at_risk_minor_units": stock_at_risk.minor_units,
        "incremental_profit_minor_units": stock_at_risk.minor_units,
        "predicted_minutes_to_unsafe": str(predicted_minutes),
    }

    return {
        "correlation_id": correlation_id,
        "scenario": _COLD_CHAIN_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict() for span in spans],
        "inference": load_inference_config().to_public_dict(),
        "seed_data": scenario.to_dict(),
        "cold_chain": {
            "site_id": site_id,
            "asset_id": asset_id,
            "category": category,
            "diagnosis": diagnosis,
            "severity": severity,
            "measured_outage_hours": str(measured_outage_hours),
            "average_temp_c": str(average_temp_c),
        },
        "learning": {
            "status": "armed",
            "message": "After approval, compare spoilage avoided and response time.",
        },
    }


def validate_recall_notice(event: Event) -> None:
    """Reject incomplete safety notices before they enter the durable event log."""
    if event.type is not EventType.RECALL_NOTICE:
        raise ValueError("event must be a recall_notice")
    required = ("recall_id", "sku", "lot_id", "reason", "issued_by")
    missing = [key for key in required if not str(event.payload.get(key) or "").strip()]
    if missing:
        raise ValueError(f"recall_notice missing fields: {missing}")
    for key in required:
        if len(str(event.payload[key])) > 200:
            raise ValueError(f"recall_notice {key} exceeds 200 characters")
    units = _int_payload(event.payload, "units", 0)
    if units <= 0:
        raise ValueError("recall_notice units must be greater than zero")


def run_recall_cascade(event: Event) -> dict[str, Any]:
    """Create a sourced, high-risk lot quarantine candidate from a recall notice."""
    validate_recall_notice(event)
    payload = event.payload
    recall_id = str(payload["recall_id"]).strip()
    sku = str(payload["sku"]).strip()
    lot_id = str(payload["lot_id"]).strip()
    reason = str(payload["reason"]).strip()
    issued_by = str(payload["issued_by"]).strip()
    issuer_verified = payload.get("issuer_verified") is True
    units = _int_payload(payload, "units", 0)
    location = str(payload.get("location") or "all_locations").strip()
    source = SourceRef.dataset("recall_notice", recall_id)
    action = RecommendedAction(
        "quarantine_lot",
        {
            "recall_id": recall_id,
            "sku": sku,
            "lot_id": lot_id,
            "units": units,
            "location": location,
            "reason": reason,
            "stop_sale": True,
            "issuer_verified": issuer_verified,
        },
        RiskTier.HIGH,
    )
    evidence = [
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"Recall {recall_id} identifies {units} units of SKU {sku}, lot {lot_id}, "
                f"for quarantine at {location}."
            ),
            supporting_data=[
                _supporting_fact("recall_id", recall_id, str(source), "supplier_recall_notice"),
                _supporting_fact("lot_id", lot_id, str(source), "supplier_recall_notice"),
                _supporting_fact("units", units, str(source), "inventory_lot_match"),
                _supporting_fact(
                    "issued_by",
                    issued_by,
                    str(source),
                    "verified_issuer" if issuer_verified else "unverified_notice_field",
                ),
            ],
            confidence=Decimal("0.99"),
            recommended_action=action,
            sources=(source,),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Recall passes the quarantine gate: recall ID, SKU, lot, reason, and affected "
                "quantity are present. Issuer identity is verified."
                if issuer_verified
                else "Recall passes the fail-safe quarantine gate, but the named issuer is not "
                "verified; manager confirmation remains required."
            ),
            supporting_data=[
                _supporting_fact(
                    "safety_gate_passed", True, "recall_gate", "required_fact_validation"
                )
            ],
            confidence=Decimal("0.99"),
            recommended_action=action,
            sources=(source, SourceRef.tool("recall_gate")),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Stop sale and quarantine lot {lot_id}; manager approval is required before "
                "the inventory write-back task is released."
            ),
            supporting_data=[
                _supporting_fact("stop_sale", True, "recall_policy", "safety_first_policy")
            ],
            confidence=Decimal("0.99"),
            recommended_action=action,
            sources=(source, SourceRef.tool("recall_policy")),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=(
            f"Pending safety approval: quarantine {units} units of SKU {sku}, lot {lot_id}, "
            f"for recall {recall_id}."
        ),
    ).to_dict()
    decision.update(
        {
            "tenant_id": event.tenant_id,
            "scenario_id": _RECALL_SCENARIO_ID,
            "role": "store_manager",
            "critic_verdict": "approved",
            "expected_outcome": {
                "recall_id": recall_id,
                "lot_id": lot_id,
                "units_quarantined": units,
                "stop_sale": True,
                "issuer_verified": issuer_verified,
                "incremental_profit_minor_units": 0,
            },
        }
    )
    return {
        "correlation_id": event.correlation_id,
        "scenario": _RECALL_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision,
        "trace": [
            TraceSpan(
                name="policy.validate_recall_notice",
                status="ok",
                ms=0,
                detail={"recall_id": recall_id, "lot_id": lot_id, "units": units},
            ).to_dict()
        ],
        "inference": load_inference_config().to_public_dict(),
        "learning": {
            "status": "armed",
            "message": "After approval, record quarantined units and recall completion time.",
        },
    }


def validate_inventory_exception(event: Event) -> None:
    """Validate evidence required by each inventory exception policy."""
    if event.type is not EventType.INVENTORY_EXCEPTION:
        raise ValueError("event must be an inventory_exception")
    payload = event.payload
    common = ("exception_id", "exception_type", "sku", "reason", "location")
    missing = [key for key in common if not str(payload.get(key) or "").strip()]
    if missing:
        raise ValueError(f"inventory_exception missing fields: {missing}")
    exception_type = str(payload["exception_type"]).strip()
    if exception_type not in _INVENTORY_EXCEPTION_TYPES:
        raise ValueError(
            "inventory_exception exception_type must be one of: "
            + ", ".join(sorted(_INVENTORY_EXCEPTION_TYPES))
        )
    for key in common:
        if len(str(payload[key])) > 200:
            raise ValueError(f"inventory_exception {key} exceeds 200 characters")
    if exception_type == "shrink":
        expected = _int_payload(payload, "expected_units", -1)
        counted = _int_payload(payload, "counted_units", -1)
        if expected < 0 or counted < 0 or counted >= expected:
            raise ValueError("shrink requires expected_units > counted_units >= 0")
        if not str(payload.get("count_reference") or "").strip():
            raise ValueError("shrink requires count_reference")
        return
    units = _int_payload(payload, "units", 0)
    if units <= 0:
        raise ValueError(f"{exception_type} units must be greater than zero")
    if exception_type == "misplaced_stock":
        expected_location = str(payload.get("expected_location") or "").strip()
        observed_location = str(payload.get("observed_location") or "").strip()
        if not expected_location or not observed_location:
            raise ValueError("misplaced_stock requires expected_location and observed_location")
        if expected_location == observed_location:
            raise ValueError("misplaced_stock locations must differ")
    elif not str(payload.get("source_reference") or "").strip():
        raise ValueError(f"{exception_type} requires source_reference")


def run_inventory_exception_cascade(event: Event) -> dict[str, Any]:
    """Route validated inventory exceptions to distinct governed actions."""
    validate_inventory_exception(event)
    payload = event.payload
    exception_id = str(payload["exception_id"]).strip()
    exception_type = str(payload["exception_type"]).strip()
    sku = str(payload["sku"]).strip()
    reason = str(payload["reason"]).strip()
    location = str(payload["location"]).strip()
    if exception_type == "shrink":
        expected_units = _int_payload(payload, "expected_units", 0)
        counted_units = _int_payload(payload, "counted_units", 0)
        units = expected_units - counted_units
        action_type = "investigate_shrink"
        action_detail = {
            "expected_units": expected_units,
            "counted_units": counted_units,
            "count_reference": str(payload["count_reference"]),
        }
    elif exception_type == "misplaced_stock":
        units = _int_payload(payload, "units", 0)
        action_type = "relocate_stock"
        action_detail = {
            "expected_location": str(payload["expected_location"]),
            "observed_location": str(payload["observed_location"]),
        }
    elif exception_type == "damage":
        units = _int_payload(payload, "units", 0)
        action_type = "quarantine_damaged_stock"
        action_detail = {"source_reference": str(payload["source_reference"])}
    else:
        units = _int_payload(payload, "units", 0)
        action_type = "process_return"
        action_detail = {"source_reference": str(payload["source_reference"])}
    source = SourceRef.dataset("inventory_exception", exception_id)
    action = RecommendedAction(
        action_type,
        {
            "exception_id": exception_id,
            "exception_type": exception_type,
            "sku": sku,
            "units": units,
            "location": location,
            "reason": reason,
            **action_detail,
        },
        RiskTier.HIGH if exception_type in {"damage", "shrink"} else RiskTier.MEDIUM,
    )
    evidence = [
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"Inventory exception {exception_id} records {units} units of SKU {sku} as "
                f"{exception_type} at {location}."
            ),
            supporting_data=[
                _supporting_fact("exception_type", exception_type, str(source), "exception_record"),
                _supporting_fact("units", units, str(source), "validated_quantity"),
                _supporting_fact("location", location, str(source), "exception_record"),
            ],
            confidence=Decimal("0.95"),
            recommended_action=action,
            sources=(source,),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                f"{exception_type} evidence satisfies its type-specific gate; route "
                f"{action_type} for human review."
            ),
            supporting_data=[
                _supporting_fact(
                    "exception_gate_passed", True, "inventory_exception_gate", exception_type
                )
            ],
            confidence=Decimal("0.96"),
            recommended_action=action,
            sources=(source, SourceRef.tool("inventory_exception_gate")),
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Assign {action_type} for {units} units; do not mutate stock before approval."
            ),
            supporting_data=[
                _supporting_fact(
                    "writeback_policy",
                    "pending_human_approval",
                    "inventory_exception_policy",
                    action_type,
                )
            ],
            confidence=Decimal("0.94"),
            recommended_action=action,
            sources=(source, SourceRef.tool("inventory_exception_policy")),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=(
            f"Pending inventory review: {action_type} for {units} units of SKU {sku} at {location}."
        ),
    ).to_dict()
    decision.update(
        {
            "tenant_id": event.tenant_id,
            "scenario_id": _INVENTORY_EXCEPTION_SCENARIO_ID,
            "role": "inventory_manager",
            "critic_verdict": "approved",
            "expected_outcome": {
                "exception_id": exception_id,
                "exception_type": exception_type,
                "units_reconciled": units,
                "incremental_profit_minor_units": 0,
            },
        }
    )
    return {
        "correlation_id": event.correlation_id,
        "scenario": _INVENTORY_EXCEPTION_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision,
        "trace": [
            TraceSpan(
                name="policy.validate_inventory_exception",
                status="ok",
                ms=0,
                detail={"exception_type": exception_type, "units": units},
            ).to_dict()
        ],
        "inference": load_inference_config().to_public_dict(),
        "learning": {
            "status": "armed",
            "message": "After approval, record reconciled units and completion time.",
        },
    }


def run_critic_rejection_cascade(*, facts: WorldFactsProvider | None = None) -> dict[str, Any]:
    """Run the planted thin-evidence case the Critic must reject."""

    correlation_id = new_id("cor")
    tenant_id = _event_tenant_id(None)
    scenario = (facts or _default_facts()).get_scenario_facts(tenant_id)
    sku = scenario.sku
    source_supplier = SourceRef.dataset("generated_world", f"suppliers:{scenario.supplier}")
    monitor = _monitor_action(sku)
    supplier_switch = RecommendedAction(
        "supplier_switch",
        {
            "sku": sku,
            "from_supplier": scenario.supplier,
            "to_supplier": "Unknown Backup Dairy",
        },
        RiskTier.HIGH,
    )
    evidence: list[EvidenceObject] = []
    spans: list[TraceSpan] = []

    started = perf_counter()
    evidence.append(
        EvidenceObject(
            agent=AgentName.OPPORTUNITY,
            conclusion=("Switch dairy supplier immediately because future delivery risk may rise."),
            supporting_data=[
                _supporting_fact(
                    "recent_delay",
                    scenario.supplier_recent_delay,
                    str(source_supplier),
                    "generated_world_supplier",
                ),
                _supporting_fact(
                    "backup_supplier_fill_rate",
                    "unknown",
                    "missing_source",
                    "not_available",
                ),
            ],
            confidence=Decimal("0.41"),
            recommended_action=supplier_switch,
            sources=(source_supplier,),
            requires_human_review=True,
        )
    )
    spans.append(
        _span(
            "critic.check_supplier_switch_evidence",
            started,
            {"verdict": "rejected"},
        )
    )

    evidence.append(
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Critic rejected supplier switch: backup supplier evidence is missing "
                "and current supplier has no recent delay."
            ),
            supporting_data=[
                _supporting_fact(
                    "critic_passed",
                    False,
                    "critic_gate",
                    "missing_backup_supplier_source",
                ),
                _supporting_fact(
                    "source_required",
                    "backup supplier fill rate",
                    "critic_gate",
                    "evidence_policy",
                ),
            ],
            confidence=Decimal("0.93"),
            recommended_action=monitor,
            sources=(SourceRef.tool("critic_gate"), source_supplier),
            requires_human_review=False,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Do not switch suppliers now. Monitor {scenario.supplier} and request "
                "sourced backup-supplier data before escalating."
            ),
            supporting_data=[
                _supporting_fact(
                    "priority",
                    "downgraded_to_monitor",
                    "executive_policy",
                    "critic_rejection",
                )
            ],
            confidence=Decimal("0.90"),
            recommended_action=monitor,
            sources=(SourceRef.tool("executive_policy"), SourceRef.tool("critic_gate")),
            requires_human_review=False,
        )
    )

    decision = Decision(
        id=new_id("dec"),
        status=DecisionStatus.REJECTED,
        action=monitor,
        caused_by=(correlation_id,),
        summary="Critic rejected supplier switch; monitor and request sourced supplier evidence.",
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = tenant_id
    decision_payload["scenario_id"] = _CRITIC_REJECTION_SCENARIO_ID
    decision_payload["role"] = "store_manager"
    decision_payload["critic_verdict"] = "rejected"
    decision_payload["rejected_action"] = supplier_switch.to_dict()

    return {
        "correlation_id": correlation_id,
        "scenario": _CRITIC_REJECTION_SCENARIO_ID,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "trace": [span.to_dict() for span in spans],
        "inference": load_inference_config().to_public_dict(),
        "seed_data": scenario.to_dict(),
        "store_intelligence": (facts or _default_facts()).get_store_intelligence(tenant_id),
        "learning": {
            "status": "critic_rejected",
            "message": (
                "No action was written back. The system downgraded to monitor until "
                "backup supplier evidence is sourced."
            ),
        },
    }


def _whole_units(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decision_id(event: Event | None) -> str:
    """Use immutable event identity for replays; mint a fresh id for manual runs."""
    return (
        f"dec_{_slug(event.tenant_id)}_{_slug(event.data_domain.value)}_{_slug(event.id)}"
        if event is not None
        else new_id("dec")
    )


def _cause_id(event: Event | None, correlation_id: str) -> str:
    return event.id if event is not None else correlation_id


def _payload_value(event: Event | None, key: str, default: object) -> object:
    if event is None:
        return default
    value = event.payload.get(key)
    return default if value is None or value == "" else value


def _int_payload(payload: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _decimal_payload(payload: dict[str, Any], key: str, default: Decimal) -> Decimal:
    try:
        return Decimal(str(payload.get(key, default)))
    except (TypeError, ValueError, InvalidOperation):
        return default


def _money_payload(value: object, *, default: Money) -> Money:
    if isinstance(value, dict):
        try:
            return Money(
                minor_units=int(value.get("minor_units", default.minor_units)),
                currency=str(value.get("currency", default.currency)),
            )
        except (TypeError, ValueError):
            return default
    if value is not None:
        try:
            return Money(minor_units=int(value), currency=default.currency)
        except (TypeError, ValueError):
            return default
    return default


def _slug(value: str) -> str:
    clean = "".join(char if char.isalnum() else "_" for char in value.strip().lower())
    return clean.strip("_") or "cold_chain_alert"
