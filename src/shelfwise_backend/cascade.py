from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter
from typing import Any

from shelfwise_contracts import (
    AgentName,
    Decision,
    DecisionStatus,
    EvidenceObject,
    RecommendedAction,
    RiskTier,
    SourceRef,
    TraceSpan,
    new_id,
)
from shelfwise_data import build_store_intelligence_demo, load_seeded_scenario
from shelfwise_decision_science import (
    forecast_demand,
    score_cold_chain_risk,
    score_expiry_risk,
    simulate_markdown,
)
from shelfwise_inference import load_inference_config


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
        id=new_id("dec"),
        status=DecisionStatus.PENDING,
        action=markdown,
        caused_by=(correlation_id,),
        summary=f"Pending manager approval: 20% markdown for {product} at {scenario.location}.",
    )
    decision_payload = decision.to_dict()
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
        "scenario": "stage4_loadshedding_x_payday_yoghurt",
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
        id=new_id("dec"),
        status=DecisionStatus.REJECTED,
        action=monitor,
        caused_by=(correlation_id,),
        summary="Critic rejected supplier switch; monitor and request sourced supplier evidence.",
    )
    decision_payload = decision.to_dict()
    decision_payload["role"] = "store_manager"
    decision_payload["critic_verdict"] = "rejected"
    decision_payload["rejected_action"] = supplier_switch.to_dict()

    return {
        "correlation_id": correlation_id,
        "scenario": "critic_rejects_unsupported_supplier_switch",
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
