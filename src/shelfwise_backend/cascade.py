from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from time import perf_counter
from typing import Any

from shelfwise_contracts import (
    AgentName,
    Decision,
    DecisionStatus,
    Event,
    EvidenceObject,
    Money,
    RecommendedAction,
    RiskTier,
    SourceRef,
    TraceSpan,
    new_id,
)
from shelfwise_data import build_store_intelligence_demo, load_seeded_scenario
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

# Scenario slugs double as the decision's stable identity (see run_golden_cascade /
# run_critic_rejection_cascade below): the demo has exactly ONE decision per scenario, and every
# /demo/* call must resolve to that SAME record, never mint a fresh duplicate. `new_id("dec")`
# used to be called per-request, which spammed the DecisionStore with a new "pending" row on
# every page load/reload. correlation_id stays random per call - that legitimately identifies
# "this cascade run", not "this decision".
_GOLDEN_SCENARIO_ID = "stage4_loadshedding_x_payday_yoghurt"
_PROCUREMENT_SCENARIO_ID = "procurement_reorder_supplier_cover"
_SALES_SCENARIO_ID = "pos_sale_price_integrity"
_COLD_CHAIN_SCENARIO_ID = "cold_chain_generator_failure_facilities_review"
_CRITIC_REJECTION_SCENARIO_ID = "critic_rejects_unsupported_supplier_switch"


def _supporting_fact(fact: str, value: object, source: str, method: str) -> dict[str, Any]:
    return {"fact": fact, "value": str(value), "source": source, "method": method}


def _span(name: str, start: float, detail: dict[str, Any] | None = None) -> TraceSpan:
    elapsed_ms = int((perf_counter() - start) * 1000)
    return TraceSpan(name=name, status="ok", ms=elapsed_ms, detail=detail or {})


def run_golden_cascade() -> dict[str, Any]:
    """Run the seeded Stage-4 load-shedding x payday yoghurt scenario.

    This is the first real vertical slice: deterministic math produces facts; product agents wrap
    those facts into evidence; the critic checks them; the executive emits one pending HITL action.
    """
    correlation_id = new_id("cor")
    scenario = load_seeded_scenario()
    sku = scenario.sku
    product = scenario.product_name
    source_stock = SourceRef.dataset("seed_stock", f"stock.csv:sku:{sku}")
    source_sales = SourceRef.dataset("seed_sales", f"sales.csv:sku:{sku}")
    source_outage = SourceRef.dataset("load_shedding", f"{scenario.location}:fridge_a")
    spans: list[TraceSpan] = []
    evidence: list[EvidenceObject] = []
    inference = load_inference_config()

    started = perf_counter()
    demand = forecast_demand(
        sku=sku,
        recent_daily_units=list(scenario.recent_daily_units),
        horizon_days=3,
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
        area="fridge_a",
        outage_hours=Decimal("3"),
        average_temp_c=Decimal("7"),
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
    simulation = simulate_markdown(
        sku=sku,
        units_on_hand=Decimal(scenario.units_on_hand),
        days_to_expiry=expiry.effective_days_to_expiry,
        base_daily_units=demand.daily_units,
        unit_price=scenario.unit_price,
        unit_cost=scenario.unit_cost,
        discount_pct=Decimal("0.20"),
    )
    markdown_margin = (scenario.unit_price * Decimal("0.80")) - scenario.unit_cost
    spans.append(
        _span(
            "decision_science.simulate_markdown",
            started,
            {"incremental_profit": str(simulation.incremental_profit)},
        )
    )

    monitor = RecommendedAction("monitor", {"sku": sku}, RiskTier.LOW)
    markdown = RecommendedAction(
        "apply_markdown",
        {"sku": sku, "discount_pct": "0.20", "duration_hours": 24},
        RiskTier.HIGH,
    )

    evidence.append(
        EvidenceObject(
            agent=AgentName.INVENTORY,
            conclusion=(
                f"{product} has {scenario.units_on_hand} units on hand "
                f"at {scenario.location}."
            ),
            supporting_data=[
                _supporting_fact(
                    "units_on_hand",
                    scenario.units_on_hand,
                    str(source_stock),
                    "seed_stock_csv",
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
                "A 20% markdown recovers more value than holding stock "
                "through the outage window."
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

    critic_passed = simulation.incremental_profit.cents > 0 and all(
        item.sources for item in evidence
    )
    critic_action = markdown if critic_passed else monitor
    evidence.append(
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=(
                "Recommendation passes: it is sourced, math-backed, "
                "and requires HITL approval."
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
            recommended_action=critic_action,
            sources=(SourceRef.tool("critic_gate"),),
            requires_human_review=True,
        )
    )
    evidence.append(
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=(
                f"Approve a 20% markdown for SKU {sku} now, then review "
                "outcome after 24 hours."
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
            recommended_action=markdown,
            sources=(SourceRef.tool("executive_policy"),),
            requires_human_review=True,
        )
    )

    decision = Decision(
        id=f"dec_{_GOLDEN_SCENARIO_ID}",
        status=DecisionStatus.PENDING,
        action=markdown,
        caused_by=(correlation_id,),
        summary=f"Pending manager approval: 20% markdown for {product} at {scenario.location}.",
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = "sa_retail_demo"
    decision_payload["role"] = "store_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"
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
        "store_intelligence": build_store_intelligence_demo(),
        "learning": {
            "status": "armed",
            "message": "After approval, compare actual sell-through with simulated sell-through.",
        },
    }


def run_procurement_cascade() -> dict[str, Any]:
    """Run the procurement role path: reorder policy plus measured supplier choice."""

    correlation_id = new_id("cor")
    scenario = load_seeded_scenario()
    sku = scenario.sku
    product = scenario.product_name
    source_stock = SourceRef.dataset("seed_stock", f"stock.csv:sku:{sku}")
    source_sales = SourceRef.dataset("seed_sales", f"sales.csv:sku:{sku}")
    source_suppliers = SourceRef.dataset("seed_suppliers", "suppliers.csv")
    spans: list[TraceSpan] = []
    evidence: list[EvidenceObject] = []

    started = perf_counter()
    policy = compute_reorder_policy(
        InventoryPolicyInput(
            sku=sku,
            on_hand=Decimal("20"),
            committed_units=Decimal("8"),
            avg_daily_demand=Decimal("10"),
            demand_std=Decimal("2"),
            lead_time_days=Decimal("3"),
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
    current_supplier = f"supplier:{scenario.supplier.lower()}"
    backup_supplier = "supplier:gauteng_chilled_dairy"
    unprofiled_supplier = "supplier:unknown_backup"
    graph.add(Relation(f"sku:{sku}", "supplied_by", current_supplier))
    graph.add(Relation(f"sku:{sku}", "supplied_by", backup_supplier))
    graph.add(Relation(f"sku:{sku}", "supplied_by", unprofiled_supplier))
    profiles = {
        current_supplier: SupplierProfile(
            supplier_id=current_supplier,
            lead_time_days=Decimal("3"),
            fill_rate=Decimal("0.76"),
            unit_cost=scenario.unit_cost,
        ),
        backup_supplier: SupplierProfile(
            supplier_id=backup_supplier,
            lead_time_days=Decimal("1"),
            fill_rate=Decimal("0.94"),
            unit_cost=Money.zar("12.80"),
        ),
    }

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
    monitor = RecommendedAction("monitor", {"sku": sku}, RiskTier.LOW)

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
                    "excluded_unprofiled_supplier",
                    unprofiled_supplier,
                    str(source_suppliers),
                    "missing_measured_profile",
                ),
            ],
            confidence=ranking.coverage,
            recommended_action=reorder,
            sources=(source_suppliers, SourceRef.tool("recommend_suppliers")),
            requires_human_review=True,
        )
    )

    critic_passed = policy.should_reorder and ranking.coverage >= Decimal("0.60")
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
            recommended_action=reorder if critic_passed else monitor,
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
            recommended_action=reorder if critic_passed else monitor,
            sources=(SourceRef.tool("executive_policy"), SourceRef.tool("critic_gate")),
            requires_human_review=critic_passed,
        )
    )

    decision = Decision(
        id=f"dec_{_PROCUREMENT_SCENARIO_ID}",
        status=DecisionStatus.PENDING if critic_passed else DecisionStatus.REJECTED,
        action=reorder if critic_passed else monitor,
        caused_by=(correlation_id,),
        summary=f"Pending procurement approval: reorder {product} from {top_supplier.supplier_id}.",
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = "sa_retail_demo"
    decision_payload["role"] = "procurement_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"
    decision_payload["expected_outcome"] = {
        "suggested_order_units": str(policy.suggested_order_units),
        "stockout_risk": str(policy.stockout_risk),
        "supplier_id": top_supplier.supplier_id,
        "supplier_coverage": str(ranking.coverage),
        "stockout_exposure": policy.zar_exposure.to_dict(),
        "stockout_exposure_minor_units": policy.zar_exposure.minor_units,
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


def run_sales_cascade(event: Event | None = None) -> dict[str, Any]:
    """Run the Sales/POS role path over a sale event and catalogue price reference."""

    correlation_id = event.correlation_id if event is not None else new_id("cor")
    scenario = load_seeded_scenario()
    sku = _payload_value(event, "sku", scenario.sku)
    location = _payload_value(event, "location", scenario.location)
    quantity = Decimal(str(_payload_value(event, "quantity", scenario.recent_daily_units[-1])))
    unit_price = Decimal(str(_payload_value(event, "unit_price", scenario.unit_price.amount)))
    expected_price = scenario.unit_price.amount
    line_revenue = Money.zar(unit_price * quantity)
    expected_revenue = scenario.unit_price * quantity
    price_delta = unit_price - expected_price
    source_pos = SourceRef.dataset("seed_sales", f"sales.csv:sku:{sku}")
    source_product = SourceRef.dataset("seed_products", f"products.csv:sku:{sku}")
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
        id=f"dec_{_slug(correlation_id)}" if event is not None else f"dec_{_SALES_SCENARIO_ID}",
        status=status,
        action=action,
        caused_by=(correlation_id,),
        summary=(
            f"POS sale for SKU {sku} recorded cleanly."
            if price_matches
            else f"Pending price exception review for SKU {sku}."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = "sa_retail_demo"
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
    source_pos = SourceRef.dataset("worldgen_pos", f"sale:{event.id}")
    source_catalog = SourceRef.dataset("worldgen_catalog", f"catalog:sku:{sku}")

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
        id=f"dec_{_slug(correlation_id)}",
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(correlation_id,),
        summary=(
            f"Pending price exception for SKU {sku}: till R{observed.amount} vs "
            f"catalogue R{catalog.amount} ({delta_display})."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = str(event.tenant_id or "sa_retail_demo")
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


def run_cold_chain_cascade(event: Event | None = None) -> dict[str, Any]:
    """Run the measured cold-chain alert path into facilities HITL review."""

    correlation_id = event.correlation_id if event is not None else new_id("cor")
    scenario = load_seeded_scenario()
    payload = event.payload if event is not None else {}
    site_id = str(payload.get("site_id") or _payload_value(event, "location", scenario.location))
    asset_id = str(payload.get("asset_id") or "fridge_dairy_1")
    category = str(payload.get("category") or "dairy")
    diagnosis = str(payload.get("diagnosis") or "generator_failed")
    severity = _int_payload(payload, "severity", 2)
    predicted_minutes = _decimal_payload(payload, "predicted_minutes_to_unsafe", Decimal("18"))
    measured_outage_hours = _decimal_payload(payload, "measured_outage_hours", Decimal("4"))
    average_temp_c = _decimal_payload(payload, "temp_c", Decimal("8.2"))
    stock_at_risk = _money_payload(
        payload.get("stock_at_risk"),
        default=Money(minor_units=643_500, currency="ZAR"),
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
        id=f"dec_{_slug(correlation_id if event is not None else _COLD_CHAIN_SCENARIO_ID)}",
        status=DecisionStatus.PENDING if alert_is_actionable else DecisionStatus.REJECTED,
        action=routed_action,
        caused_by=(correlation_id,),
        summary=(
            f"Pending facilities review for {asset_id}: {diagnosis} with "
            f"{stock_at_risk} at risk."
            if alert_is_actionable
            else f"Monitor {asset_id}; no measured cold-chain intervention required."
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = event.tenant_id if event is not None else "sa_retail_demo"
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


def run_critic_rejection_cascade() -> dict[str, Any]:
    """Run the planted thin-evidence case the Critic must reject."""

    correlation_id = new_id("cor")
    scenario = load_seeded_scenario()
    sku = scenario.sku
    source_supplier = SourceRef.dataset("seed_suppliers", f"suppliers.csv:{scenario.supplier}")
    monitor = RecommendedAction("monitor", {"sku": sku}, RiskTier.LOW)
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
            conclusion=(
                "Switch dairy supplier immediately because future delivery risk may rise."
            ),
            supporting_data=[
                _supporting_fact(
                    "recent_delay",
                    scenario.supplier_recent_delay,
                    str(source_supplier),
                    "seed_supplier_csv",
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
        id=f"dec_{_CRITIC_REJECTION_SCENARIO_ID}",
        status=DecisionStatus.REJECTED,
        action=monitor,
        caused_by=(correlation_id,),
        summary="Critic rejected supplier switch; monitor and request sourced supplier evidence.",
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = "sa_retail_demo"
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
        "store_intelligence": build_store_intelligence_demo(),
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
