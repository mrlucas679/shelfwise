from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    run_cold_chain_cascade_via_agents,
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


def _scripted_messages() -> list[dict[str, Any]]:
    return [
        _tool_call_message(
            "call_1",
            "get_cold_chain_status",
            {"area": "fridge_dairy_1", "outage_hours": 4.0, "average_temp_c": 8.2},
        ),
        _final_message(
            {
                "conclusion": (
                    "Measured cold-chain risk is at maximum severity (confidence 0.78 in "
                    "this assessment) - meaningfully elevated, warranting a facilities "
                    "dispatch."
                ),
                "confidence": 0.85,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": "Dispatch a facilities check given the 0.78-confidence assessment.",
                "confidence": 0.84,
                "recommended_action_type": "dispatch_facilities_check",
            }
        ),
    ]


def test_agentic_cold_chain_cascade_drives_real_tool_calls_and_produces_decision() -> None:
    tools, decisions, memory, facts = _build_tools()
    runtime = _FakeRuntime(_scripted_messages())

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_cold_chain_cascade_via_agents(
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
    assert tool_names_called == {"get_cold_chain_status"}

    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "cold_chain")
    executive_evidence = next(e for e in result["evidence"] if e["agent"] == "executive")
    assert "0.78" in critic_evidence["conclusion"]
    assert executive_evidence["recommended_action"]["type"] == "dispatch_facilities_check"

    decision = result["decision"]
    assert decision["status"] == "pending"
    assert decision["scenario_id"] == "cold_chain_generator_failure_facilities_review"
    assert decision["role"] == "facilities_manager"


def test_agentic_cold_chain_cascade_hard_fails_when_live_required_sees_offline_provider() -> None:
    tools, decisions, memory, facts = _build_tools()
    runtime = _FakeRuntime(
        _scripted_messages(),
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=False,
        provider="offline",
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError):
        run_cold_chain_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.LIVE_REQUIRED,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )


def test_agentic_cold_chain_cascade_rejects_a_conclusion_that_cites_no_real_numbers() -> None:
    tools, decisions, memory, facts = _build_tools()
    ungrounded_messages = [
        _tool_call_message(
            "call_1",
            "get_cold_chain_status",
            {"area": "fridge_dairy_1", "outage_hours": 4.0, "average_temp_c": 8.2},
        ),
        _final_message(
            {
                "conclusion": "The situation looks risky so we should dispatch someone.",
                "confidence": 0.85,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
    ]
    runtime = _FakeRuntime(ungrounded_messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError, match="get_cold_chain_status"):
        run_cold_chain_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.OFFLINE_TEST,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )
