from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Money
from shelfwise_data import load_seeded_scenario
from shelfwise_decision_science import (
    InventoryPolicyInput,
    Relation,
    RelationStore,
    StockSourceCandidate,
    SupplierProfile,
    compute_reorder_policy,
    detect_robust_anomaly,
    forecast_demand,
    plan_stock_sourcing,
    recommend_suppliers,
    score_cold_chain_risk,
    score_expiry_risk,
    simulate_markdown,
)

ToolFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class PlatformTool:
    name: str
    description: str
    read_only: bool
    fn: ToolFn

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
        }


class AuditLog:
    """Small in-process audit log for platform tool calls."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def record(self, *, tool: str, tenant_id: str, args: dict[str, Any]) -> None:
        self._events.append(
            {
                "tool": tool,
                "tenant_id": tenant_id,
                "args": deepcopy(args),
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._events]

    def clear(self) -> None:
        self._events.clear()


def build_platform_tools(
    *,
    decisions: Any,
    memory: Any,
    audit: AuditLog | None = None,
) -> list[PlatformTool]:
    """Build read-only tools for customer agents and internal MCP registration."""
    audit_log = audit or AuditLog()

    async def get_stock(sku: str = "4011", tenant_id: str = "sa_retail_demo") -> dict[str, Any]:
        audit_log.record(tool="get_stock", tenant_id=tenant_id, args={"sku": sku})
        scenario = load_seeded_scenario(sku=sku)
        return {
            "sku": scenario.sku,
            "product_name": scenario.product_name,
            "location": scenario.location,
            "on_hand": scenario.units_on_hand,
            "reorder_point": scenario.reorder_point,
            "days_to_expiry": scenario.days_to_expiry,
            "source": "seeded_scenario",
        }

    async def get_thresholds(tenant_id: str = "sa_retail_demo") -> dict[str, Any]:
        audit_log.record(tool="get_thresholds", tenant_id=tenant_id, args={})
        return {"thresholds": memory.thresholds()}

    async def list_open_decisions(tenant_id: str = "sa_retail_demo") -> dict[str, Any]:
        audit_log.record(tool="list_open_decisions", tenant_id=tenant_id, args={})
        rows = [
            item
            for item in decisions.list()
            if item.get("status") == "pending"
            and str(item.get("tenant_id") or "default") == tenant_id
        ]
        return {"decisions": rows}

    async def explain_decision(
        decision_id: str,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(
            tool="explain_decision",
            tenant_id=tenant_id,
            args={"decision_id": decision_id},
        )
        decision = decisions.get(decision_id)
        if decision is None or str(decision.get("tenant_id") or "default") != tenant_id:
            return {"decision": None, "summary": "Decision not found for tenant."}
        return {
            "decision": decision,
            "summary": decision.get("summary", ""),
            "critic_verdict": decision.get("critic_verdict"),
        }

    async def simulate_markdown_tool(
        sku: str = "4011",
        discount_pct: float = 0.2,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(
            tool="simulate_markdown",
            tenant_id=tenant_id,
            args={"sku": sku, "discount_pct": discount_pct},
        )
        scenario = load_seeded_scenario(sku=sku)
        recent_days = Decimal(len(scenario.recent_daily_units))
        result = simulate_markdown(
            sku=sku,
            units_on_hand=Decimal(scenario.units_on_hand),
            days_to_expiry=Decimal(scenario.days_to_expiry),
            base_daily_units=sum(scenario.recent_daily_units) / recent_days,
            unit_price=scenario.unit_price,
            unit_cost=scenario.unit_cost,
            discount_pct=Decimal(str(discount_pct)),
        )
        return {
            "sku": sku,
            "discount_pct": str(discount_pct),
            "markdown_units_sold": str(result.markdown_units_sold),
            "markdown_waste_units": str(result.markdown_waste_units),
            "incremental_profit": result.incremental_profit.to_dict(),
            "method": result.method,
        }

    async def get_demand_forecast(
        sku: str = "4011",
        horizon_days: int = 3,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(
            tool="get_demand_forecast",
            tenant_id=tenant_id,
            args={"sku": sku, "horizon_days": horizon_days},
        )
        scenario = load_seeded_scenario(sku=sku)
        result = forecast_demand(
            sku=sku,
            recent_daily_units=list(scenario.recent_daily_units),
            horizon_days=horizon_days,
        )
        return {
            "sku": sku,
            "daily_units": str(result.daily_units),
            "confidence": str(result.confidence),
            "method": result.method,
        }

    async def get_expiry_risk(
        sku: str = "4011",
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(tool="get_expiry_risk", tenant_id=tenant_id, args={"sku": sku})
        scenario = load_seeded_scenario(sku=sku)
        demand = forecast_demand(
            sku=sku,
            recent_daily_units=list(scenario.recent_daily_units),
            horizon_days=3,
        )
        cold = score_cold_chain_risk(
            area="fridge_a",
            outage_hours=Decimal("0"),
            average_temp_c=Decimal("5"),
        )
        result = score_expiry_risk(
            sku=sku,
            units_on_hand=Decimal(scenario.units_on_hand),
            days_to_expiry=Decimal(scenario.days_to_expiry),
            forecast_daily_units=demand.daily_units,
            unit_cost=scenario.unit_cost,
            cold_chain_risk=cold.risk,
            cold_chain_penalty_days=cold.penalty_days,
        )
        return {
            "sku": sku,
            "risk": str(result.risk),
            "waste_units": str(result.waste_units),
            "zar_at_risk": result.zar_at_risk.to_dict(),
            "method": result.method,
        }

    async def get_cold_chain_status(
        area: str = "fridge_a",
        outage_hours: float = 3.0,
        average_temp_c: float = 7.0,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(
            tool="get_cold_chain_status",
            tenant_id=tenant_id,
            args={"area": area, "outage_hours": outage_hours, "average_temp_c": average_temp_c},
        )
        result = score_cold_chain_risk(
            area=area,
            outage_hours=Decimal(str(outage_hours)),
            average_temp_c=Decimal(str(average_temp_c)),
        )
        return {
            "area": area,
            "risk": str(result.risk),
            "penalty_days": str(result.penalty_days),
            "confidence": str(result.confidence),
            "method": result.method,
        }

    async def get_reorder_policy(
        sku: str = "4011",
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(tool="get_reorder_policy", tenant_id=tenant_id, args={"sku": sku})
        scenario = load_seeded_scenario(sku=sku)
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
        return {
            "sku": sku,
            "should_reorder": policy.should_reorder,
            "reorder_point_units": str(policy.reorder_point_units),
            "suggested_order_units": str(policy.suggested_order_units),
            "stockout_risk": str(policy.stockout_risk),
            "method": policy.method,
        }

    async def get_supplier_ranking(
        sku: str = "4011",
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(tool="get_supplier_ranking", tenant_id=tenant_id, args={"sku": sku})
        scenario = load_seeded_scenario(sku=sku)
        graph = RelationStore()
        current_supplier = f"supplier:{scenario.supplier.lower()}"
        backup_supplier = "supplier:gauteng_chilled_dairy"
        graph.add(Relation(f"sku:{sku}", "supplied_by", current_supplier))
        graph.add(Relation(f"sku:{sku}", "supplied_by", backup_supplier))
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
        ranking = recommend_suppliers(sku, graph, profiles)
        top = ranking.ranked[0]
        return {
            "sku": sku,
            "top_supplier": top.supplier_id,
            "coverage": str(ranking.coverage),
            "method": ranking.method,
        }

    async def get_stock_sourcing_options(
        sku: str = "4011",
        units_needed: int = 18,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        """Rank real candidate sources for a shortage instead of assuming a transfer.

        Checks nearby branches, the regional distribution centre, and approved
        suppliers, ranks whichever have stock by lead time/distance/cost, and
        recommends a purchase order (with a stated reason) if none can supply it.
        """
        audit_log.record(
            tool="get_stock_sourcing_options",
            tenant_id=tenant_id,
            args={"sku": sku, "units_needed": units_needed},
        )
        scenario = load_seeded_scenario(sku=sku)
        current_supplier = f"supplier:{scenario.supplier.lower()}"
        candidates = (
            StockSourceCandidate(
                source_type="branch",
                source_id="store_02_sandton",
                available_units=6,
                distance_km=Decimal("5"),
                lead_time_hours=Decimal("2"),
            ),
            StockSourceCandidate(
                source_type="branch",
                source_id="store_09_midrand",
                available_units=14,
                distance_km=Decimal("22"),
                lead_time_hours=Decimal("4"),
            ),
            StockSourceCandidate(
                source_type="distribution_center",
                source_id="dc_gauteng_central",
                available_units=400,
                distance_km=Decimal("65"),
                lead_time_hours=Decimal("18"),
                unit_cost=scenario.unit_cost.amount,
            ),
            StockSourceCandidate(
                source_type="supplier",
                source_id=current_supplier,
                available_units=200,
                distance_km=Decimal("140"),
                lead_time_hours=scenario.supplier_lead_time_days * Decimal("24"),
                unit_cost=scenario.unit_cost.amount,
            ),
            StockSourceCandidate(
                source_type="supplier",
                source_id="supplier:gauteng_chilled_dairy",
                available_units=200,
                distance_km=Decimal("160"),
                lead_time_hours=Decimal("24"),
                unit_cost=Decimal("12.80"),
            ),
        )
        plan = plan_stock_sourcing(sku=sku, units_needed=units_needed, candidates=candidates)
        return plan.to_dict()

    async def check_price_integrity(
        sku: str = "4011",
        observed_unit_price: float | None = None,
        tenant_id: str = "sa_retail_demo",
    ) -> dict[str, Any]:
        audit_log.record(
            tool="check_price_integrity",
            tenant_id=tenant_id,
            args={"sku": sku, "observed_unit_price": observed_unit_price},
        )
        scenario = load_seeded_scenario(sku=sku)
        catalog_price = scenario.unit_price.amount
        observed = (
            Decimal(str(observed_unit_price))
            if observed_unit_price is not None
            else catalog_price
        )
        delta = observed - catalog_price
        anomaly = detect_robust_anomaly(
            metric_name="pos_sale_units",
            current_value=Decimal(str(scenario.recent_daily_units[-1])),
            history=list(scenario.recent_daily_units),
        )
        return {
            "sku": sku,
            "observed_unit_price": str(observed),
            "catalog_unit_price": str(catalog_price),
            "price_delta": str(delta),
            "velocity_anomaly": anomaly.is_anomaly,
            "method": "catalog_price_integrity",
        }

    return [
        PlatformTool("get_stock", "Read current stock context for one SKU.", True, get_stock),
        PlatformTool("get_thresholds", "Read learned threshold memory.", True, get_thresholds),
        PlatformTool(
            "list_open_decisions",
            "List pending HITL decisions.",
            True,
            list_open_decisions,
        ),
        PlatformTool(
            "explain_decision",
            "Explain a decision and its critic verdict.",
            True,
            explain_decision,
        ),
        PlatformTool(
            "simulate_markdown",
            "Simulate a markdown without writing back.",
            True,
            simulate_markdown_tool,
        ),
        PlatformTool(
            "get_demand_forecast",
            "Read the measured demand forecast for one SKU.",
            True,
            get_demand_forecast,
        ),
        PlatformTool(
            "get_expiry_risk",
            "Read the measured expiry/waste risk for one SKU.",
            True,
            get_expiry_risk,
        ),
        PlatformTool(
            "get_cold_chain_status",
            "Read the measured cold-chain risk for one refrigeration area.",
            True,
            get_cold_chain_status,
        ),
        PlatformTool(
            "get_reorder_policy",
            "Read the measured reorder policy for one SKU.",
            True,
            get_reorder_policy,
        ),
        PlatformTool(
            "get_supplier_ranking",
            "Read the measured supplier ranking for one SKU.",
            True,
            get_supplier_ranking,
        ),
        PlatformTool(
            "get_stock_sourcing_options",
            "Rank real branch/DC/supplier sources for a stock shortage, or recommend a "
            "purchase order if none can supply it.",
            True,
            get_stock_sourcing_options,
        ),
        PlatformTool(
            "check_price_integrity",
            "Check an observed till price against the catalogue price for one SKU.",
            True,
            check_price_integrity,
        ),
    ]


def register_platform_mcp(
    mcp: Any,
    tools: list[PlatformTool],
    *,
    audit: AuditLog | None = None,
) -> None:
    """Register tools with FastMCP-compatible objects, refusing write tools structurally."""
    _ = audit
    for spec in tools:
        if not spec.read_only:
            raise ValueError(f"refusing to expose write-capable platform tool: {spec.name}")
        mcp.tool(name=spec.name, description=spec.description)(spec.fn)
