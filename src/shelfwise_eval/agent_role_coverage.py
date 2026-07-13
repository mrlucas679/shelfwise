from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from shelfwise_action import create_decision_store
from shelfwise_backend.tools.mcp_surface import AuditLog, PlatformTool, build_platform_tools
from shelfwise_backend.tools.model_runtime import (
    OpenAIModelRuntime,
    architecture_from_inference_config,
)
from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_inference.config import load_inference_config
from shelfwise_inference.orchestration import (
    AgentOrchestrationError,
    AgentOrchestrator,
    AgentRunResult,
    ExecutionMode,
)
from shelfwise_inference.tool_calling import (
    ToolCallingError,
    assert_conclusion_grounded_in_tool_results,
)
from shelfwise_memory import create_learning_store
from shelfwise_worldgen import create_world_snapshot_store

# The provider can, intermittently, stall producing the final JSON object even after a
# genuine tool-calling round trip completed (a decoding-loop failure mode observed live
# against Gemma-4-E4B-it/vLLM). Both this and any orchestration-level failure must be
# recorded as an honest per-role failure, never allowed to crash the whole coverage run.
_ROLE_FAILURE_EXCEPTIONS: tuple[type[Exception], ...] = (AgentOrchestrationError, ToolCallingError)

# Proves the mandate's "every role receives measurable inference work in evaluation mode"
# requirement across all eleven named agent roles, not just the two (Critic/Executive) wired
# into the golden cascade's production HITL path. Each role gets a genuine Gemma tool-calling
# round trip against a role-relevant read-only tool backed by real decision-science math.

_ROLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requires_human_review": {"type": "boolean"},
    },
    "required": ["conclusion", "confidence", "requires_human_review"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class RolePrompt:
    role: str
    system: str
    user: str
    expected_tool: str | None
    required_tools: tuple[str, ...] = ()


_ROLE_PROMPTS: tuple[RolePrompt, ...] = (
    RolePrompt(
        "inventory",
        "You are the ShelfWise Inventory agent. Call get_stock for the SKU before concluding.",
        "Assess the stock position for SKU {sku}. Call get_stock, then conclude.",
        "get_stock",
    ),
    RolePrompt(
        "sales",
        "You are the ShelfWise Sales agent. Call check_price_integrity before concluding.",
        "Check whether SKU {sku}'s till price matches catalogue. Call check_price_integrity, "
        "then conclude.",
        "check_price_integrity",
    ),
    RolePrompt(
        "cold_chain",
        "You are the ShelfWise Cold Chain agent. Call get_cold_chain_status before concluding.",
        "Assess cold-chain risk for fridge_a with a 3 hour outage at 7C. Call "
        "get_cold_chain_status with area=fridge_a, outage_hours=3, average_temp_c=7, "
        "then conclude.",
        "get_cold_chain_status",
    ),
    RolePrompt(
        "expiry",
        "You are the ShelfWise Expiry agent. Call get_expiry_risk before concluding.",
        "Assess expiry/waste risk for SKU {sku}. Call get_expiry_risk, then conclude.",
        "get_expiry_risk",
    ),
    RolePrompt(
        "demand",
        "You are the ShelfWise Demand agent. Call get_demand_forecast before concluding.",
        "Forecast demand for SKU {sku} over the next 3 days. Call get_demand_forecast, "
        "then conclude.",
        "get_demand_forecast",
    ),
    RolePrompt(
        "procurement",
        "You are the ShelfWise Procurement agent. Call get_reorder_policy and "
        "get_supplier_ranking before concluding.",
        "Decide whether to reorder SKU {sku} and from which supplier. Call "
        "get_reorder_policy, then get_supplier_ranking, then conclude.",
        "get_reorder_policy",
    ),
    RolePrompt(
        "opportunity",
        "You are the ShelfWise Opportunity agent. Call simulate_markdown before concluding.",
        "Evaluate whether a 20% markdown on SKU {sku} recovers more value than holding stock. "
        "Call simulate_markdown with discount_pct=0.2, then conclude.",
        "simulate_markdown",
    ),
    RolePrompt(
        "simulation",
        "You are the ShelfWise Simulation agent. Call simulate_markdown before concluding.",
        "Simulate the sell-through and waste outcome of a 20% markdown on SKU {sku}. Call "
        "simulate_markdown with discount_pct=0.2, then conclude.",
        "simulate_markdown",
    ),
    RolePrompt(
        "critic",
        "You are the ShelfWise Critic agent. Call list_open_decisions to find the open "
        "decisions, then call explain_decision on the first decision's id before concluding.",
        "Review the current open HITL decisions for evidence quality. Call "
        "list_open_decisions, then call explain_decision with the first decision's id, "
        "then conclude. Cite the pending 20% markdown value from the open decision in "
        "your conclusion.",
        "explain_decision",
        ("list_open_decisions", "explain_decision"),
    ),
    RolePrompt(
        "executive",
        "You are the ShelfWise Executive agent. Call get_thresholds before concluding.",
        "Review the current learned thresholds before deciding today's priorities. Call "
        "get_thresholds, then conclude.",
        "get_thresholds",
    ),
    RolePrompt(
        "orchestrator",
        "You are the ShelfWise Orchestrator agent. Call list_open_decisions before concluding.",
        "Summarize the current queue of open decisions across roles. Call "
        "list_open_decisions, then conclude and cite the pending 20% markdown value.",
        "list_open_decisions",
    ),
)


@dataclass(frozen=True, slots=True)
class RoleCoverageResult:
    role: str
    ok: bool
    expected_tool: str | None
    tools_called: tuple[str, ...]
    answer: dict[str, Any] | None
    model_call_count: int
    total_tokens: int
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ok": self.ok,
            "expected_tool": self.expected_tool,
            "expected_tool_called": self.expected_tool in self.tools_called
            if self.expected_tool
            else None,
            "tools_called": list(self.tools_called),
            "answer": self.answer,
            "model_call_count": self.model_call_count,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


def run_agent_role_coverage(
    *, execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED
) -> list[RoleCoverageResult]:
    """Send one real, tool-backed Gemma call per named agent role and report the results."""
    return asyncio.run(_run(execution_mode=execution_mode))


async def _run(*, execution_mode: ExecutionMode) -> list[RoleCoverageResult]:
    decisions = create_decision_store()
    memory = create_learning_store()
    audit = AuditLog()
    facts = WorldFactsProvider(create_world_snapshot_store())
    tenant_id = "eval_tenant"
    hero_sku = facts.get_hero_sku(tenant_id)
    hero = facts.get_scenario_facts(tenant_id, hero_sku)
    # One real pending decision so list_open_decisions returns a non-empty queue and
    # explain_decision has a genuine id to resolve - without it the critic's
    # explain_decision coverage would be untestable against an empty store.
    decisions.upsert(
        {
            "id": "dec_role_coverage_seed",
            "status": "pending",
            "action": {
                "type": "apply_markdown",
                "params": {"sku": hero_sku, "discount_pct": "0.20"},
                "risk_tier": "high",
            },
            "caused_by": ["role_coverage_seed"],
            "summary": (
                f"Pending manager approval: 20% markdown for {hero.product_name} "
                f"at {hero.location}."
            ),
            "tenant_id": tenant_id,
            "critic_verdict": "approved",
        }
    )
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=audit,
        facts=facts,
        tenant_id=tenant_id,
    )
    config = load_inference_config()
    architecture = architecture_from_inference_config(config)
    runtime = OpenAIModelRuntime(architecture=architecture, execution_mode=execution_mode)
    orchestrator = AgentOrchestrator(tools=tools, model_runtime=runtime)

    results: list[RoleCoverageResult] = []
    for prompt in _ROLE_PROMPTS:
        results.append(await _run_one(orchestrator, prompt, tenant_id=tenant_id, sku=hero_sku))
    return results


async def _run_one(
    orchestrator: AgentOrchestrator,
    prompt: RolePrompt,
    *,
    tenant_id: str,
    sku: str,
) -> RoleCoverageResult:
    try:
        run_result: AgentRunResult = await orchestrator.run(
            role=prompt.role,
            system=prompt.system,
            user=prompt.user.format(sku=sku),
            final_schema=_ROLE_SCHEMA,
            final_schema_name=f"{prompt.role}_role_coverage",
            temperature=0.0,
            # The orchestrator's trusted tenant override wins over model-invented tenant
            # arguments, so this must match the tenant the seed decision was written under.
            tenant_id=tenant_id,
            require_tool_call_first=prompt.expected_tool is not None,
            required_tool_names=prompt.required_tools,
        )
        if isinstance(run_result.answer, dict):
            assert_conclusion_grounded_in_tool_results(
                str(run_result.answer.get("conclusion", "")), run_result.tool_calls
            )
    except _ROLE_FAILURE_EXCEPTIONS as exc:
        return RoleCoverageResult(
            role=prompt.role,
            ok=False,
            expected_tool=prompt.expected_tool,
            tools_called=(),
            answer=None,
            model_call_count=0,
            total_tokens=0,
            error=str(exc),
        )
    total_tokens = sum(call.input_tokens + call.output_tokens for call in run_result.model_calls)
    tools_called = tuple(call.name for call in run_result.tool_calls)
    if prompt.expected_tool is not None and prompt.expected_tool not in tools_called:
        return RoleCoverageResult(
            role=prompt.role,
            ok=False,
            expected_tool=prompt.expected_tool,
            tools_called=tools_called,
            answer=dict(run_result.answer) if isinstance(run_result.answer, dict) else None,
            model_call_count=len(run_result.model_calls),
            total_tokens=total_tokens,
            error=f"expected tool was not called: {prompt.expected_tool}",
        )
    return RoleCoverageResult(
        role=prompt.role,
        ok=True,
        expected_tool=prompt.expected_tool,
        tools_called=tools_called,
        answer=dict(run_result.answer) if isinstance(run_result.answer, dict) else None,
        model_call_count=len(run_result.model_calls),
        total_tokens=total_tokens,
        error=None,
    )
