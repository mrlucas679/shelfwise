from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_decision_science import (
    InventoryPolicyInput,
    Relation,
    RelationStore,
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
from shelfwise_runtime import DataDomain, normalize_domain
from shelfwise_twin import TwinService

from ..product_catalog import get_delivery_exception
from ..world_facts import WorldFactsProvider

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

    def record(
        self,
        *,
        tool: str,
        tenant_id: str,
        args: dict[str, Any],
        data_domain: str | None = None,
    ) -> None:
        resolved_domain = data_domain or (
            "operational_twin"
            if tool.startswith(("live_", "get_live_"))
            else "world_simulation"
        )
        self._events.append(
            {
                "tool": tool,
                "tenant_id": tenant_id,
                "data_domain": resolved_domain,
                "args": deepcopy(args),
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    def list(
        self,
        *,
        tenant_id: str | None = None,
        data_domain: str | None = None,
    ) -> list[dict[str, Any]]:
        events = self._events
        if tenant_id is not None:
            events = [item for item in events if item.get("tenant_id") == tenant_id]
        if data_domain is not None:
            events = [item for item in events if item.get("data_domain") == data_domain]
        return [deepcopy(item) for item in events]

    def clear(self) -> None:
        self._events.clear()


def build_platform_tools(
    *,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider,
    tenant_id: str,
    audit: AuditLog | None = None,
) -> list[PlatformTool]:
    """Build read-only tools for customer agents and internal MCP registration."""
    if not tenant_id.strip():
        raise ValueError("tenant_id is required to build platform tools")
    audit_log = audit or AuditLog()
    data_domain = normalize_domain(
        getattr(facts, "data_domain", DataDomain.WORLD_SIMULATION.value),
        default=DataDomain.WORLD_SIMULATION,
    )

    def record_tool(tool: str, args: dict[str, Any]) -> None:
        audit_log.record(
            tool=tool,
            tenant_id=tenant_id,
            args=args,
            data_domain=data_domain,
        )

    async def get_stock(sku: str | None = None) -> dict[str, Any]:
        record_tool("get_stock", {"sku": sku})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        return {
            "data_domain": data_domain,
            "sku": scenario.sku,
            "product_name": scenario.product_name,
            "location": scenario.location,
            "on_hand": scenario.units_on_hand,
            "reorder_point": scenario.reorder_point,
            "days_to_expiry": scenario.days_to_expiry,
            "source": str(getattr(facts, "source_dataset", "generated_world")),
        }

    async def get_thresholds() -> dict[str, Any]:
        record_tool("get_thresholds", {})
        return {
            "data_domain": data_domain,
            "thresholds": memory.thresholds(
                tenant_id=tenant_id,
                data_domain=data_domain,
            )
        }

    async def list_open_decisions() -> dict[str, Any]:
        record_tool("list_open_decisions", {})
        rows = [
            item
            for item in decisions.list()
            if item.get("status") == "pending"
            and str(item.get("tenant_id") or "default") == tenant_id
            and str(item.get("data_domain") or DataDomain.WORLD_SIMULATION.value)
            == data_domain
        ]
        return {"data_domain": data_domain, "decisions": rows}

    async def explain_decision(
        decision_id: str,
    ) -> dict[str, Any]:
        record_tool("explain_decision", {"decision_id": decision_id})
        decision = decisions.get(decision_id)
        if (
            decision is None
            or str(decision.get("tenant_id") or "default") != tenant_id
            or str(decision.get("data_domain") or DataDomain.WORLD_SIMULATION.value)
            != data_domain
        ):
            return {
                "data_domain": data_domain,
                "decision": None,
                "summary": "Decision not found for tenant.",
            }
        return {
            "data_domain": data_domain,
            "decision": decision,
            "summary": decision.get("summary", ""),
            "critic_verdict": decision.get("critic_verdict"),
        }

    async def simulate_markdown_tool(
        sku: str | None = None,
        discount_pct: float = 0.2,
    ) -> dict[str, Any]:
        record_tool("simulate_markdown", {"sku": sku, "discount_pct": discount_pct})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        recent_days = Decimal(len(scenario.recent_daily_units))
        result = simulate_markdown(
            sku=scenario.sku,
            units_on_hand=Decimal(scenario.units_on_hand),
            days_to_expiry=Decimal(scenario.days_to_expiry),
            base_daily_units=sum(scenario.recent_daily_units) / recent_days,
            unit_price=scenario.unit_price,
            unit_cost=scenario.unit_cost,
            discount_pct=Decimal(str(discount_pct)),
        )
        return {
            "sku": scenario.sku,
            "discount_pct": str(discount_pct),
            "markdown_units_sold": str(result.markdown_units_sold),
            "markdown_waste_units": str(result.markdown_waste_units),
            "incremental_profit": result.incremental_profit.to_dict(),
            "method": result.method,
        }

    async def get_demand_forecast(
        sku: str | None = None,
        horizon_days: int = 3,
    ) -> dict[str, Any]:
        record_tool("get_demand_forecast", {"sku": sku, "horizon_days": horizon_days})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        result = forecast_demand(
            sku=scenario.sku,
            recent_daily_units=list(scenario.recent_daily_units),
            horizon_days=horizon_days,
            payday_multiplier=Decimal("1"),
        )
        return {
            "sku": scenario.sku,
            "daily_units": str(result.daily_units),
            "confidence": str(result.confidence),
            "method": result.method,
        }

    async def get_expiry_risk(
        sku: str | None = None, days_to_expiry: int | None = None
    ) -> dict[str, Any]:
        record_tool("get_expiry_risk", {"sku": sku, "days_to_expiry": days_to_expiry})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        expiry_days = (
            Decimal(str(days_to_expiry))
            if days_to_expiry is not None
            else Decimal(scenario.days_to_expiry)
        )
        demand = forecast_demand(
            sku=scenario.sku,
            recent_daily_units=list(scenario.recent_daily_units),
            horizon_days=3,
            payday_multiplier=Decimal("1"),
        )
        cold = score_cold_chain_risk(
            area="fridge_a",
            outage_hours=Decimal("0"),
            average_temp_c=Decimal("5"),
        )
        result = score_expiry_risk(
            sku=scenario.sku,
            units_on_hand=Decimal(scenario.units_on_hand),
            days_to_expiry=expiry_days,
            forecast_daily_units=demand.daily_units,
            unit_cost=scenario.unit_cost,
            cold_chain_risk=cold.risk,
            cold_chain_penalty_days=cold.penalty_days,
        )
        return {
            "sku": scenario.sku,
            "days_to_expiry": str(expiry_days),
            "risk": str(result.risk),
            "waste_units": str(result.waste_units),
            "zar_at_risk": result.zar_at_risk.to_dict(),
            "forecast_daily_units": str(demand.daily_units),
            "method": result.method,
        }

    async def get_cold_chain_status(
        area: str = "fridge_a",
        outage_hours: float = 3.0,
        average_temp_c: float = 7.0,
    ) -> dict[str, Any]:
        record_tool(
            "get_cold_chain_status",
            {"area": area, "outage_hours": outage_hours, "average_temp_c": average_temp_c},
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

    async def get_reorder_policy(sku: str | None = None) -> dict[str, Any]:
        record_tool("get_reorder_policy", {"sku": sku})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        recent = [Decimal(value) for value in scenario.recent_daily_units] or [Decimal("1")]
        avg_daily_demand = sum(recent) / Decimal(len(recent))
        variance = sum((value - avg_daily_demand) ** 2 for value in recent) / Decimal(len(recent))
        demand_std = variance.sqrt() if variance > 0 else Decimal("0")
        policy = compute_reorder_policy(
            InventoryPolicyInput(
                sku=scenario.sku,
                on_hand=Decimal(scenario.units_on_hand),
                committed_units=Decimal("0"),
                avg_daily_demand=avg_daily_demand,
                demand_std=demand_std,
                lead_time_days=scenario.supplier_lead_time_days,
                unit_cost=scenario.unit_cost,
            )
        )
        return {
            "sku": scenario.sku,
            "should_reorder": policy.should_reorder,
            "reorder_point_units": str(policy.reorder_point_units),
            "suggested_order_units": str(policy.suggested_order_units),
            "stockout_risk": str(policy.stockout_risk),
            "method": policy.method,
        }

    async def get_supplier_ranking(sku: str | None = None) -> dict[str, Any]:
        record_tool("get_supplier_ranking", {"sku": sku})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        current = facts.get_supplier_for_sku(tenant_id, scenario.sku)
        graph = RelationStore()
        current_supplier = current["supplier_id"]
        graph.add(Relation(f"sku:{scenario.sku}", "supplied_by", current_supplier))
        profiles = {
            current_supplier: SupplierProfile(
                supplier_id=current_supplier,
                lead_time_days=Decimal(str(current["lead_time_days"])),
                fill_rate=Decimal(str(current["fill_rate"])),
                unit_cost=scenario.unit_cost,
            ),
        }
        alternate = facts.get_alternate_supplier(tenant_id, exclude=current_supplier)
        if alternate is not None:
            backup_supplier = alternate["supplier_id"]
            graph.add(Relation(f"sku:{scenario.sku}", "supplied_by", backup_supplier))
            profiles[backup_supplier] = SupplierProfile(
                supplier_id=backup_supplier,
                lead_time_days=Decimal(str(alternate["lead_time_days"])),
                fill_rate=Decimal(str(alternate["fill_rate"])),
                unit_cost=scenario.unit_cost,
            )
        ranking = recommend_suppliers(scenario.sku, graph, profiles)
        top = ranking.ranked[0]
        return {
            "sku": scenario.sku,
            "top_supplier": top.supplier_id,
            "coverage": str(ranking.coverage),
            "method": ranking.method,
        }

    async def get_stock_sourcing_options(
        sku: str | None = None,
        units_needed: int = 18,
    ) -> dict[str, Any]:
        """Rank real candidate sources for a shortage instead of assuming a transfer.

        Checks nearby branches, the regional distribution centre, and approved
        suppliers, ranks whichever have stock by lead time/distance/cost, and
        recommends a purchase order (with a stated reason) if none can supply it.
        """
        record_tool("get_stock_sourcing_options", {"sku": sku, "units_needed": units_needed})
        scenario = facts.get_scenario_facts(tenant_id, sku)
        candidates = facts.get_sourcing_candidates(tenant_id, scenario.sku)
        plan = plan_stock_sourcing(
            sku=scenario.sku, units_needed=units_needed, candidates=candidates
        )
        return plan.to_dict()

    async def check_price_integrity(
        sku: str | None = None,
        observed_unit_price: float | None = None,
    ) -> dict[str, Any]:
        record_tool(
            "check_price_integrity",
            {"sku": sku, "observed_unit_price": observed_unit_price},
        )
        scenario = facts.get_scenario_facts(tenant_id, sku)
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
            "sku": scenario.sku,
            "observed_unit_price": str(observed),
            "catalog_unit_price": str(catalog_price),
            "price_delta": str(delta),
            "velocity_anomaly": anomaly.is_anomaly,
            "method": "catalog_price_integrity",
        }

    async def get_delivery_status(sku: str | None = None) -> dict[str, Any]:
        """What a specific delivery for one SKU actually looks like: ordered vs. received vs.
        accepted, short/over/rejected units, and the resulting supplier fill rate - by real
        product name, not just a code, so an operator asking "what's expected from this
        delivery" gets the same individual receiving record the Deliveries workspace shows."""
        record_tool("get_delivery_status", {"sku": sku})
        resolved_sku = sku or facts.get_hero_sku(tenant_id)
        exception = get_delivery_exception(facts=facts, tenant_id=tenant_id, sku=resolved_sku)
        if exception is not None:
            return exception
        scenario = facts.get_scenario_facts(tenant_id, resolved_sku)
        return {
            "sku": scenario.sku,
            "product_name": scenario.product_name,
            "status": "no_exception",
            "conclusion": f"{scenario.product_name} has no open delivery exception right now.",
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
        PlatformTool(
            "get_delivery_status",
            "Read the individual delivery reconciliation for one SKU, by real product name.",
            True,
            get_delivery_status,
        ),
    ]


def build_live_twin_tools(
    *,
    decisions: Any,
    memory: Any,
    twin: TwinService,
    tenant_id: str,
    audit: AuditLog | None = None,
) -> list[PlatformTool]:
    """Build live-only tools that cannot read generated-world or scenario data."""
    audit_log = audit or AuditLog()

    async def get_live_twin_state(
        store_id: str | None = None, property_name: str | None = None
    ) -> dict[str, Any]:
        audit_log.record(
            tool="get_live_twin_state",
            tenant_id=tenant_id,
            args={"store_id": store_id, "property_name": property_name},
        )
        return twin.live_context(
            tenant_id,
            store_id=store_id,
            property_name=property_name,
        )

    async def get_live_stock(sku: str | None = None, store_id: str | None = None) -> dict[str, Any]:
        """Read reported inventory from the operational twin only."""
        audit_log.record(
            tool="get_live_stock",
            tenant_id=tenant_id,
            args={"sku": sku, "store_id": store_id},
            data_domain=DataDomain.OPERATIONAL_TWIN.value,
        )
        result = twin.live_context(
            tenant_id,
            store_id=store_id,
            property_name="inventory.on_hand",
            entity_local_id=sku,
        )
        result["requested_sku"] = sku
        return result

    async def get_live_cold_chain_status(
        store_id: str | None = None, property_name: str = "cold_chain.diagnosis"
    ) -> dict[str, Any]:
        """Read reported cold-chain observations without simulated defaults."""
        return await get_live_twin_state(store_id, property_name)

    async def list_open_decisions() -> dict[str, Any]:
        audit_log.record(tool="live_list_open_decisions", tenant_id=tenant_id, args={})
        return {
            "data_domain": "operational_twin",
            "decisions": [
                item for item in decisions.list()
                if item.get("status") == "pending"
                and str(item.get("tenant_id") or "default") == tenant_id
                and item.get("data_domain") == "operational_twin"
            ],
        }

    async def explain_decision(decision_id: str) -> dict[str, Any]:
        audit_log.record(
            tool="live_explain_decision", tenant_id=tenant_id, args={"decision_id": decision_id}
        )
        decision = decisions.get(decision_id)
        if (
            decision is None
            or str(decision.get("tenant_id") or "default") != tenant_id
            or decision.get("data_domain") != "operational_twin"
        ):
            return {"data_domain": "operational_twin", "decision": None}
        return {"data_domain": "operational_twin", "decision": decision}

    async def get_thresholds() -> dict[str, Any]:
        audit_log.record(tool="live_get_thresholds", tenant_id=tenant_id, args={})
        return {
            "data_domain": "operational_twin",
            "thresholds": memory.thresholds(
                tenant_id=tenant_id,
                data_domain="operational_twin",
            ),
        }

    return [
        PlatformTool(
            "get_live_twin_state", "Read reported state from the exact operational shop.",
            True, get_live_twin_state,
        ),
        PlatformTool(
            "get_live_stock", "Read reported inventory from the operational twin.",
            True, get_live_stock,
        ),
        PlatformTool(
            "get_live_cold_chain_status",
            "Read reported cold-chain state from the operational twin.",
            True, get_live_cold_chain_status,
        ),
        PlatformTool(
            "live_list_open_decisions", "List pending decisions for this tenant.",
            True, list_open_decisions,
        ),
        PlatformTool(
            "live_explain_decision", "Explain one tenant-scoped decision.", True, explain_decision,
        ),
        PlatformTool(
            "live_get_thresholds", "Read learned thresholds and policies.", True, get_thresholds,
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
