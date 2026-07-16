from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    run_sales_cascade_via_agents,
)
from shelfwise_backend.tools.mcp_surface import AuditLog, build_platform_tools
from shelfwise_backend.world_facts import WorldFactsProvider
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


def _hero_price_facts(facts: WorldFactsProvider) -> tuple[str, str, str, str]:
    """Compute the real catalogue price and a 20%-over observed price (matches the
    cascade's own mismatch construction: observed_unit_price = catalog_price * 1.2)."""
    scenario = facts.get_scenario_facts("sa_retail_demo")
    catalog_price = scenario.unit_price.amount
    observed_price = round(float(catalog_price) * 1.2, 2)
    delta = round(observed_price - float(catalog_price), 2)
    return scenario.sku, str(observed_price), str(catalog_price), str(delta)


def _scripted_messages(sku: str, observed: str, catalog: str, delta: str) -> list[dict[str, Any]]:
    return [
        _tool_call_message(
            "call_1", "check_price_integrity", {"sku": sku, "observed_unit_price": float(observed)}
        ),
        _final_message(
            {
                "conclusion": (
                    f"Observed price {observed} differs from catalogue price {catalog} by "
                    f"a delta of {delta}, so this must be routed for manager review."
                ),
                "confidence": 0.83,
                "critic_passed": False,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": f"Route the price exception at {observed} vs {catalog} for review.",
                "confidence": 0.82,
                "recommended_action_type": "review_price_exception",
            }
        ),
    ]


def test_agentic_sales_cascade_drives_real_tool_calls_and_produces_decision() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, observed, catalog, delta = _hero_price_facts(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, observed, catalog, delta))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_sales_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    assert result["agentic"] is True
    assert len(runtime.requests) == 3, "expected a real critic tool call plus an executive call"

    tool_names_called = {call["name"] for call in result["tool_calls"]}
    assert tool_names_called == {"check_price_integrity"}

    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "sales")
    executive_evidence = next(e for e in result["evidence"] if e["agent"] == "executive")
    assert observed in critic_evidence["conclusion"]
    assert executive_evidence["recommended_action"]["type"] == "review_price_exception"

    decision = result["decision"]
    assert decision["status"] == "pending"
    assert decision["scenario_id"] == "pos_sale_price_integrity"
    assert decision["role"] == "sales_manager"


def test_approving_an_agentic_sales_price_exception_produces_real_learning_movement() -> None:
    """`Decision` objects built by the agentic cascades never carried `expected_outcome`,
    so approving one always computed zero exposure in `record_approved_decision` - invisible
    to every past test because nothing had ever approved an agentic decision and checked the
    learning store afterward (found 2026-07-15 by distrusting a run that reported 0 failures).
    """
    tools, decisions, memory, facts = _build_tools()
    sku, observed, catalog, delta = _hero_price_facts(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, observed, catalog, delta))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_sales_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )
    assert result["decision"]["action"]["type"] == "review_price_exception"

    decisions.upsert(result["decision"])
    decision = decisions.approve(result["decision"]["id"])
    assert decision is not None

    learning_event = memory.record_approved_decision(decision)
    assert learning_event["delta_units"] > 0, (
        "approving an agentic price-exception decision must move a real learning "
        "threshold, not silently record zero exposure"
    )
    assert learning_event["outcome"]["measured_minor_units"] > 0


def test_agentic_sales_cascade_hard_fails_when_live_required_sees_offline_provider() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, observed, catalog, delta = _hero_price_facts(facts)
    runtime = _FakeRuntime(
        _scripted_messages(sku, observed, catalog, delta),
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=False,
        provider="offline",
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError):
        run_sales_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.LIVE_REQUIRED,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )


def test_agentic_sales_cascade_rejects_a_conclusion_that_cites_no_real_numbers() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, observed, _catalog, _delta = _hero_price_facts(facts)
    ungrounded_messages = [
        _tool_call_message(
            "call_1", "check_price_integrity", {"sku": sku, "observed_unit_price": float(observed)}
        ),
        _final_message(
            {
                "conclusion": "The price seems off so this should go to review.",
                "confidence": 0.83,
                "critic_passed": False,
                "requires_human_review": True,
            }
        ),
    ]
    runtime = _FakeRuntime(ungrounded_messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError, match="check_price_integrity"):
        run_sales_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.OFFLINE_TEST,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )
