from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_data import load_seeded_scenario
from shelfwise_decision_science import simulate_markdown

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
