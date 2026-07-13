from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    run_catalog_price_check_via_agents,
    run_expiry_risk_check_via_agents,
)
from shelfwise_backend.tools.mcp_surface import AuditLog, PlatformTool, build_platform_tools
from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_contracts import Event, EventType
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


def _build_tools() -> tuple[list[PlatformTool], Any, Any, WorldFactsProvider]:
    decisions = create_decision_store()
    memory = create_learning_store()
    audit = AuditLog()
    facts = WorldFactsProvider(InMemoryWorldSnapshotStore())
    tools = build_platform_tools(
        decisions=decisions, memory=memory, audit=audit, facts=facts, tenant_id="sa_retail_demo"
    )
    return tools, decisions, memory, facts


def _run_tool(tools: list[PlatformTool], name: str, **kwargs: Any) -> dict[str, Any]:
    tool = next(item for item in tools if item.name == name)
    return asyncio.run(tool.fn(**kwargs))


def _price_event(facts: WorldFactsProvider) -> Event:
    scenario = facts.get_scenario_facts("sa_retail_demo")
    observed_minor = int((Decimal(scenario.unit_price.minor_units) * Decimal("1.20")).quantize(
        Decimal("1")
    ))
    return Event(
        id="evt_agentic_price_guardrail",
        type=EventType.SALE,
        ts=datetime.now(UTC),
        actor="test",
        tenant_id="sa_retail_demo",
        correlation_id="cor_agentic_price_guardrail",
        payload={
            "sku": scenario.sku,
            "units": 2,
            "unit_price_cents": observed_minor,
            "catalog_price_cents": scenario.unit_price.minor_units,
        },
    )


def _expiry_event(facts: WorldFactsProvider) -> Event:
    scenario = facts.get_scenario_facts("sa_retail_demo")
    return Event(
        id="evt_agentic_expiry_guardrail",
        type=EventType.EXPIRY_ENTRY,
        ts=datetime.now(UTC),
        actor="test",
        tenant_id="sa_retail_demo",
        correlation_id="cor_agentic_expiry_guardrail",
        payload={
            "sku": scenario.sku,
            "batch_id": f"BATCH-{scenario.sku}",
            "days_to_expiry": 1,
        },
    )


def test_agentic_catalog_price_guardrail_calls_price_tool_and_mints_review() -> None:
    tools, decisions, memory, facts = _build_tools()
    event = _price_event(facts)
    observed = Decimal(int(event.payload["unit_price_cents"])) / Decimal("100")
    price_result = _run_tool(
        tools,
        "check_price_integrity",
        sku=str(event.payload["sku"]),
        observed_unit_price=float(observed),
    )
    messages = [
        _tool_call_message(
            "call_1",
            "check_price_integrity",
            {"sku": event.payload["sku"], "observed_unit_price": float(observed)},
        ),
        _final_message(
            {
                "conclusion": (
                    f"Observed price {price_result['observed_unit_price']} differs from "
                    f"catalogue price {price_result['catalog_unit_price']} by "
                    f"{price_result['price_delta']}, so manager review is required."
                ),
                "confidence": 0.84,
                "critic_passed": False,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": (
                    f"Route review_price_exception because catalogue price "
                    f"{price_result['catalog_unit_price']} was breached."
                ),
                "confidence": 0.82,
                "recommended_action_type": "review_price_exception",
            }
        ),
    ]
    runtime = _FakeRuntime(messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_catalog_price_check_via_agents(
        event,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    assert result is not None
    assert result["agentic"] is True
    assert {call["name"] for call in result["tool_calls"]} == {"check_price_integrity"}
    assert result["scenario"] == "pos_price_outlier_review"
    assert result["decision"]["action"]["type"] == "review_price_exception"
    assert result["decision"]["role"] == "sales_manager"


def test_agentic_expiry_guardrail_calls_expiry_tool_and_mints_review() -> None:
    tools, decisions, memory, facts = _build_tools()
    event = _expiry_event(facts)
    expiry_result = _run_tool(
        tools,
        "get_expiry_risk",
        sku=str(event.payload["sku"]),
        days_to_expiry=int(event.payload["days_to_expiry"]),
    )
    messages = [
        _tool_call_message(
            "call_1",
            "get_expiry_risk",
            {"sku": event.payload["sku"], "days_to_expiry": event.payload["days_to_expiry"]},
        ),
        _final_message(
            {
                "conclusion": (
                    f"Expiry risk is {expiry_result['risk']} with waste units "
                    f"{expiry_result['waste_units']}, so inventory review is required."
                ),
                "confidence": 0.86,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": (
                    f"Route review_expiry_markdown because risk {expiry_result['risk']} "
                    "is inside the review window."
                ),
                "confidence": 0.83,
                "recommended_action_type": "review_expiry_markdown",
            }
        ),
    ]
    runtime = _FakeRuntime(messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_expiry_risk_check_via_agents(
        event,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    assert result is not None
    assert result["agentic"] is True
    assert {call["name"] for call in result["tool_calls"]} == {"get_expiry_risk"}
    assert result["scenario"] == "expiry_risk_markdown_review"
    assert result["decision"]["action"]["type"] == "review_expiry_markdown"
    assert result["decision"]["role"] == "inventory_manager"


def test_agentic_guardrail_hard_fails_when_live_required_sees_offline_provider() -> None:
    tools, decisions, memory, facts = _build_tools()
    event = _price_event(facts)
    observed = Decimal(int(event.payload["unit_price_cents"])) / Decimal("100")
    price_result = _run_tool(
        tools,
        "check_price_integrity",
        sku=str(event.payload["sku"]),
        observed_unit_price=float(observed),
    )
    runtime = _FakeRuntime(
        [
            _tool_call_message(
                "call_1",
                "check_price_integrity",
                {"sku": event.payload["sku"], "observed_unit_price": float(observed)},
            ),
            _final_message(
                {
                    "conclusion": (
                        f"Catalogue price {price_result['catalog_unit_price']} was breached."
                    ),
                    "confidence": 0.84,
                    "critic_passed": False,
                    "requires_human_review": True,
                }
            ),
        ],
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=False,
        provider="offline",
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError):
        run_catalog_price_check_via_agents(
            event,
            execution_mode=ExecutionMode.LIVE_REQUIRED,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )
