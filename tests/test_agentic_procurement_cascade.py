from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    run_procurement_cascade_via_agents,
)
from shelfwise_backend.tools.mcp_surface import AuditLog, build_platform_tools
from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_decision_science import InventoryPolicyInput, compute_reorder_policy
from shelfwise_inference.orchestration import (
    AgentArchitecture,
    AgentOrchestrator,
    ArchitectureMode,
    ExecutionMode,
    ModelCall,
    RoleModelTarget,
)
from shelfwise_memory import create_learning_store
from shelfwise_worldgen.world_store import InMemoryWorldSnapshotStore


@dataclass
class _FakeRuntime:
    """Deterministic stand-in provider that scripts a real tool-calling round trip."""

    messages: list[dict[str, Any]]
    mode: ExecutionMode = ExecutionMode.OFFLINE_TEST
    used_network: bool = False
    provider: str = "deterministic_fake"
    fallback: bool = False

    def __post_init__(self) -> None:
        self.architecture = AgentArchitecture(
            mode=ArchitectureMode.SHARED,
            default_target=RoleModelTarget("fake://runtime", "gemma-fake"),
        )
        self.execution_mode = self.mode
        self.requests: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> ModelCall:
        self.requests.append(deepcopy(kwargs))
        index = len(self.requests) - 1
        message = self.messages[index]
        return ModelCall(
            call_id=f"model_{index + 1}",
            role=kwargs["role"],
            message=message,
            provider=self.provider,
            model="gemma-fake",
            endpoint="fake://runtime",
            used_network=self.used_network,
            input_tokens=10 + index,
            output_tokens=5 + index,
            latency_ms=3 + index,
            correlation_id=kwargs["correlation_id"],
            finish_reason="tool_calls" if "tool_calls" in message else "stop",
            fallback=self.fallback,
        )


def _tool_call_message(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _final_message(payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": json.dumps(payload)}


def _build_tools():
    decisions = create_decision_store()
    memory = create_learning_store()
    audit = AuditLog()
    facts = WorldFactsProvider(InMemoryWorldSnapshotStore())
    tools = build_platform_tools(
        decisions=decisions, memory=memory, audit=audit, facts=facts, tenant_id="sa_retail_demo"
    )
    return tools, decisions, memory, facts


def _hero_procurement_facts(facts: WorldFactsProvider) -> tuple[str, str, str]:
    """Compute the real reorder-policy figure and chosen supplier the fake LLM cites."""
    scenario = facts.get_scenario_facts("sa_retail_demo")
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
    current_supplier = facts.get_supplier_for_sku("sa_retail_demo", scenario.sku)
    alternate = facts.get_alternate_supplier(
        "sa_retail_demo", exclude=current_supplier["supplier_id"]
    )
    chosen_supplier = (
        alternate["supplier_id"] if alternate is not None else current_supplier["supplier_id"]
    )
    return scenario.sku, str(policy.suggested_order_units), chosen_supplier


def _scripted_messages(sku: str, order_units: str, supplier_id: str) -> list[dict[str, Any]]:
    return [
        _tool_call_message("call_1", "get_reorder_policy", {"sku": sku}),
        _tool_call_message("call_2", "get_supplier_ranking", {"sku": sku}),
        _final_message(
            {
                "conclusion": (
                    f"Reorder policy suggests {order_units} units; {supplier_id} has the "
                    "best measured coverage."
                ),
                "confidence": 0.86,
                "critic_passed": True,
                "supplier_id": supplier_id,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": f"Route the {order_units} unit reorder to procurement for approval.",
                "confidence": 0.84,
                "recommended_action_type": "reorder",
            }
        ),
    ]


def test_agentic_procurement_cascade_drives_real_tool_calls_and_produces_decision() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, order_units, supplier_id = _hero_procurement_facts(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, order_units, supplier_id))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_procurement_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    assert result["agentic"] is True
    assert len(runtime.requests) == 4, "expected a real critic tool loop plus an executive call"

    tool_names_called = {call["name"] for call in result["tool_calls"]}
    assert tool_names_called == {"get_reorder_policy", "get_supplier_ranking"}, (
        "the agentic procurement cascade must actually execute the registered read-only "
        "tools, not just claim to"
    )

    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "critic")
    executive_evidence = next(e for e in result["evidence"] if e["agent"] == "executive")
    assert order_units in critic_evidence["conclusion"]
    assert executive_evidence["recommended_action"]["type"] == "reorder"
    assert executive_evidence["recommended_action"]["params"]["supplier_id"] == supplier_id

    decision = result["decision"]
    assert decision["status"] == "pending"
    assert decision["scenario_id"] == "procurement_reorder_supplier_cover"
    assert decision["role"] == "procurement_manager"


def test_agentic_procurement_cascade_hard_fails_when_live_required_sees_offline_provider() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, order_units, supplier_id = _hero_procurement_facts(facts)
    runtime = _FakeRuntime(
        _scripted_messages(sku, order_units, supplier_id),
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=False,
        provider="offline",
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError):
        run_procurement_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.LIVE_REQUIRED,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )


def test_agentic_procurement_cascade_rejects_a_conclusion_that_cites_no_real_numbers() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, _order_units, supplier_id = _hero_procurement_facts(facts)
    ungrounded_messages = [
        _tool_call_message("call_1", "get_reorder_policy", {"sku": sku}),
        _tool_call_message("call_2", "get_supplier_ranking", {"sku": sku}),
        _final_message(
            {
                "conclusion": "The supplier situation looks fine so we should reorder.",
                "confidence": 0.86,
                "critic_passed": True,
                "supplier_id": supplier_id,
                "requires_human_review": True,
            }
        ),
    ]
    runtime = _FakeRuntime(ungrounded_messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError, match="get_reorder_policy"):
        run_procurement_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.OFFLINE_TEST,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )
