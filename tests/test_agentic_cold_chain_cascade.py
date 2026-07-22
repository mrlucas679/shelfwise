from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    _bounded_conclusion,
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


def test_approving_an_agentic_cold_chain_decision_produces_real_learning_movement() -> None:
    """`Decision` objects built by the agentic cascades never carried `expected_outcome`,
    so approving one always computed zero exposure in `record_approved_decision` - invisible
    to every past test because nothing had ever approved an agentic decision and checked the
    learning store afterward (found 2026-07-15 by distrusting a run that reported 0 failures).
    """
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
    assert result["decision"]["action"]["type"] == "dispatch_facilities_check"

    decisions.upsert(result["decision"])
    decision = decisions.approve(result["decision"]["id"])
    assert decision is not None

    learning_event = memory.record_approved_decision(decision)
    assert learning_event["delta_units"] > 0, (
        "approving an agentic dispatch decision must move a real learning threshold, not "
        "silently record zero exposure because expected_outcome was never populated"
    )
    assert learning_event["outcome"]["measured_minor_units"] > 0


def test_agentic_cold_chain_decision_economics_shows_real_recovered_value_not_zero() -> None:
    """`_attach_decision_governance` (the `/mlops` decision-economics dashboard's data
    source) reads only `expected_outcome["incremental_profit_minor_units"]` - a different
    key than the one the learning-store fix populates. Without it, an agentic cold-chain
    dispatch still displayed "R0.00 recovered" on the dashboard even after learning
    movement was fixed, because nothing had ever run the real economics-attachment
    function against a real agentic result and checked its output.
    """
    from shelfwise_backend.app import _attach_decision_governance

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

    _attach_decision_governance(result)

    economics = result["decision"]["economics"]
    assert economics["recovered"]["minor_units"] > 0
    assert economics["recovered"]["minor_units"] == result["decision"]["expected_outcome"][
        "incremental_profit_minor_units"
    ]


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


def test_agentic_cold_chain_cascade_caps_max_tokens_and_reports_a_token_budget_receipt() -> None:
    """SLO-fit finding (2026-07-14 forensic audit): cold-chain was the other cascade that
    reproducibly blew the 30s hackathon ceiling at the measured ~19 effective tok/s. Every
    call must request the 400-token cap, and the result must carry a `token_budget` receipt.
    """
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

    assert all(request["max_tokens"] == 400 for request in runtime.requests)
    expected_prompt = sum(call["usage"]["input_tokens"] for call in result["model_calls"])
    expected_completion = sum(call["usage"]["output_tokens"] for call in result["model_calls"])
    assert result["token_budget"] == {
        "prompt_tokens": expected_prompt,
        "completion_tokens": expected_completion,
        "calls": len(result["model_calls"]),
    }


def test_agentic_cold_chain_cascade_bounds_a_long_conclusion_before_executive_prompt() -> None:
    """Forwarding the critic's full prose into the executive's strong-tier prompt duplicates
    evidence for no decision-relevant gain - the executive prompt must shrink even when the
    critic writes a maximally long conclusion, while the recorded evidence keeps the critic's
    full, untrimmed conclusion.
    """
    tools, decisions, memory, facts = _build_tools()
    long_conclusion = (
        "Measured cold-chain risk is at maximum severity (confidence 0.78 in this "
        "assessment) - meaningfully elevated, warranting a facilities dispatch. "
        + ("Additional narrative padding restating the same verdict. " * 6)
    )
    assert len(long_conclusion) > 240
    messages = [
        _tool_call_message(
            "call_1",
            "get_cold_chain_status",
            {"area": "fridge_dairy_1", "outage_hours": 4.0, "average_temp_c": 8.2},
        ),
        _final_message(
            {
                "conclusion": long_conclusion,
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
    runtime = _FakeRuntime(messages)

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

    assert runtime.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_cold_chain_status"},
    }
    executive_user_message = runtime.requests[2]["messages"][1]["content"]
    expected_bounded = _bounded_conclusion(long_conclusion)
    assert expected_bounded != long_conclusion
    assert len(expected_bounded) <= len(long_conclusion) * 0.60
    assert repr(expected_bounded) in executive_user_message
    assert repr(long_conclusion) not in executive_user_message
    assert len(executive_user_message) < len(long_conclusion)

    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "cold_chain")
    assert critic_evidence["conclusion"] == long_conclusion


def test_critic_gate_overrides_a_cold_chain_executive_that_dispatches_past_a_failed_critic() -> (
    None
):
    """Same binding-verdict contract as golden, proven independently for cold-chain.

    A hallucinating executive answering `dispatch_facilities_check` despite a critic
    that measured the alert as not actionable is the failure mode the gate exists to
    close here. Only the golden cascade had this proof before; a regression that
    stopped routing cold-chain's executive action through the gate would have shipped
    an unwarranted HIGH-risk facilities dispatch undetected.
    """
    tools, decisions, memory, facts = _build_tools()
    disagreeing_messages = [
        _tool_call_message(
            "call_1",
            "get_cold_chain_status",
            {"area": "fridge_dairy_1", "outage_hours": 4.0, "average_temp_c": 8.2},
        ),
        _final_message(
            {
                "conclusion": (
                    "Measured cold-chain risk is 0.78 but stock-at-risk value is negligible - "
                    "does not clear the bar for an actionable alert."
                ),
                "confidence": 0.9,
                "critic_passed": False,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": "Dispatch a facilities check anyway, better safe than sorry.",
                "confidence": 0.5,
                "recommended_action_type": "dispatch_facilities_check",
            }
        ),
    ]
    runtime = _FakeRuntime(disagreeing_messages)

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

    decision = result["decision"]
    assert decision["action"]["type"] == "monitor_cold_chain", (
        "a failed critic verdict must force monitoring even when the executive answers "
        "with a HIGH-risk facilities dispatch"
    )
    assert decision["critic_gate"] == {
        "critic_passed": False,
        "executive_action_type": "dispatch_facilities_check",
        "override_applied": True,
    }


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
