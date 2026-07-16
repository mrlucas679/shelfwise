from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from shelfwise_action import create_decision_store
from shelfwise_backend import agentic_cascade as agentic_cascade_module
from shelfwise_backend.agentic_cascade import (
    AgenticCascadeError,
    _bounded_conclusion,
    run_golden_cascade_via_agents,
)
from shelfwise_backend.tools.mcp_surface import AuditLog, build_platform_tools
from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_contracts import Event, EventType
from shelfwise_decision_science import simulate_markdown as compute_markdown
from shelfwise_inference.orchestration import (
    AgentArchitecture,
    AgentOrchestrator,
    ArchitectureMode,
    ExecutionMode,
    ModelCall,
    RoleModelTarget,
)
from shelfwise_memory import create_learning_store
from shelfwise_runtime import DataDomain
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


def _hero_markdown_profit(facts: WorldFactsProvider) -> tuple[str, str, str]:
    """Compute the real markdown-simulation figures the fake LLM is scripted to cite."""
    scenario = facts.get_scenario_facts("sa_retail_demo")
    recent_days = Decimal(len(scenario.recent_daily_units))
    result = compute_markdown(
        sku=scenario.sku,
        units_on_hand=Decimal(scenario.units_on_hand),
        days_to_expiry=Decimal(scenario.days_to_expiry),
        base_daily_units=sum(scenario.recent_daily_units) / recent_days,
        unit_price=scenario.unit_price,
        unit_cost=scenario.unit_cost,
        discount_pct=Decimal("0.2"),
    )
    return scenario.sku, str(scenario.units_on_hand), str(result.incremental_profit.amount)


def _scripted_messages(sku: str, on_hand: str, profit: str) -> list[dict[str, Any]]:
    return [
        _tool_call_message("call_1", "get_stock", {"sku": sku}),
        _tool_call_message("call_2", "simulate_markdown", {"sku": sku, "discount_pct": 0.2}),
        _final_message(
            {
                "conclusion": (
                    f"{on_hand} units on hand and a simulated incremental profit of "
                    f"R{profit} support a markdown."
                ),
                "confidence": 0.87,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": "Route the markdown to the store manager for approval.",
                "confidence": 0.85,
                "recommended_action_type": "apply_markdown",
            }
        ),
    ]


def test_agentic_golden_cascade_drives_real_tool_calls_and_produces_decision() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
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
    assert tool_names_called == {"get_stock", "simulate_markdown"}, (
        "the agentic cascade must actually execute the registered read-only tools, "
        "not just claim to"
    )

    assert len(result["model_calls"]) == 4
    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "critic")
    executive_evidence = next(e for e in result["evidence"] if e["agent"] == "executive")
    assert profit in critic_evidence["conclusion"]
    assert executive_evidence["recommended_action"]["type"] == "apply_markdown"

    decision = result["decision"]
    assert decision["status"] == "pending"
    assert decision["scenario_id"] == "stage4_loadshedding_x_payday_yoghurt"


def test_operational_agentic_result_and_model_receipt_keep_live_domain() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))
    event = Event(
        id="evt_agentic_scope",
        type=EventType.SCAN,
        ts=datetime(2026, 7, 13, 9, tzinfo=UTC),
        actor="store_1",
        tenant_id="sa_retail_demo",
        data_domain=DataDomain.OPERATIONAL_TWIN,
        payload={"sku": sku},
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=event,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )
    recorded: list[dict[str, Any]] = []
    recorder = agentic_cascade_module._scoped_recorder(recorded.append, event)
    assert recorder is not None
    recorder({"id": "mr_scope"})

    assert result["tenant_id"] == "sa_retail_demo"
    assert result["data_domain"] == "operational_twin"
    assert result["decision"]["data_domain"] == "operational_twin"
    assert recorded == [{"id": "mr_scope", "data_domain": "operational_twin"}]


def test_agentic_golden_cascade_hard_fails_when_live_required_sees_offline_provider() -> None:
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(
        _scripted_messages(sku, on_hand, profit),
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=False,
        provider="offline",
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError):
        run_golden_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.LIVE_REQUIRED,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )


def test_agentic_golden_cascade_rejects_a_conclusion_that_cites_no_real_numbers() -> None:
    """A model can call the real calculator tools and then still write a vague, ungrounded
    conclusion instead of citing what it computed - that must be caught, not passed through.
    """
    tools, decisions, memory, facts = _build_tools()
    sku, _on_hand, _profit = _hero_markdown_profit(facts)
    ungrounded_messages = [
        _tool_call_message("call_1", "get_stock", {"sku": sku}),
        _tool_call_message("call_2", "simulate_markdown", {"sku": sku, "discount_pct": 0.2}),
        _final_message(
            {
                "conclusion": "The numbers look good so a markdown makes sense here.",
                "confidence": 0.87,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
    ]
    runtime = _FakeRuntime(ungrounded_messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    with pytest.raises(AgenticCascadeError, match="get_stock"):
        run_golden_cascade_via_agents(
            event=None,
            execution_mode=ExecutionMode.OFFLINE_TEST,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=factory,
        )


def test_agentic_golden_cascade_caps_max_tokens_and_reports_a_token_budget_receipt() -> None:
    """SLO-fit finding (2026-07-14 forensic audit): golden's prior 800-token verdict default
    made it arithmetically unable to finish inside the 30s hackathon ceiling at the measured
    ~19 effective tok/s. Every call must request the 400-token cap, and the result must carry
    a `token_budget` receipt so future regressions are visible without waiting for a timeout.
    """
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
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


def test_agentic_golden_cascade_bounds_a_long_critic_conclusion_before_the_executive_prompt() -> (
    None
):
    """The critic schema allows up to 600 chars of prose. Forwarding all of it verbatim into
    the executive's strong-tier prompt just duplicates it for no decision-relevant gain - the
    executive prompt must shrink even when the critic writes a maximally long conclusion,
    while the recorded evidence keeps the critic's full, untrimmed conclusion.
    """
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    long_conclusion = (
        f"{on_hand} units on hand and a simulated incremental profit of R{profit} support "
        "a markdown. " + ("Additional narrative padding restating the same verdict. " * 6)
    )
    assert len(long_conclusion) > 240
    messages = [
        _tool_call_message("call_1", "get_stock", {"sku": sku}),
        _tool_call_message("call_2", "simulate_markdown", {"sku": sku, "discount_pct": 0.2}),
        _final_message(
            {
                "conclusion": long_conclusion,
                "confidence": 0.87,
                "critic_passed": True,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": "Route the markdown to the store manager for approval.",
                "confidence": 0.85,
                "recommended_action_type": "apply_markdown",
            }
        ),
    ]
    runtime = _FakeRuntime(messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    executive_user_message = runtime.requests[3]["messages"][1]["content"]
    expected_bounded = _bounded_conclusion(long_conclusion)
    assert expected_bounded != long_conclusion
    assert len(expected_bounded) <= len(long_conclusion) * 0.60
    assert repr(expected_bounded) in executive_user_message
    assert repr(long_conclusion) not in executive_user_message
    assert len(executive_user_message) < len(long_conclusion), (
        "the executive prompt must shrink relative to an unbounded critic conclusion"
    )

    critic_evidence = next(e for e in result["evidence"] if e["agent"] == "critic")
    assert critic_evidence["conclusion"] == long_conclusion, (
        "the trim applies only to the prompt forwarded to the executive - the recorded "
        "evidence must still keep the critic's full conclusion"
    )


def test_approving_an_agentic_golden_decision_produces_real_learning_movement() -> None:
    """`Decision` objects built by the agentic cascades never carried `expected_outcome`,
    so approving one always computed zero exposure in `record_approved_decision` - a real,
    previously-undiscovered defect across the entire agentic path, invisible to every past
    test because nothing had ever approved an agentic decision and checked the learning
    store afterward (found 2026-07-15 by distrusting a run that reported 0 failures).
    """
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    decisions.upsert(result["decision"])
    decision = decisions.approve(result["decision"]["id"])
    assert decision is not None
    assert decision["status"] == "approved"

    learning_event = memory.record_approved_decision(decision)
    assert learning_event["delta_units"] > 0, (
        "approving an agentic decision must move a real learning threshold, not silently "
        "record zero exposure because expected_outcome was never populated"
    )
    assert learning_event["outcome"]["units_cleared"] > 0


def test_agentic_golden_decision_economics_shows_real_recovered_value_not_zero() -> None:
    """`_attach_decision_governance` (the `/mlops` decision-economics dashboard's data
    source) reads only `expected_outcome["incremental_profit_minor_units"]` - a different
    key than the one the learning-store fix populates. Without it, an agentic golden
    decision's real, successful markdown still displayed "R0.00 recovered" on the
    dashboard even after learning movement was fixed, because nothing had ever run the
    real economics-attachment function against a real agentic result and checked its
    output (found 2026-07-15 by distrusting a run that reported 0 failures).
    """
    from shelfwise_backend.app import _attach_decision_governance

    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    _attach_decision_governance(result)

    economics = result["decision"]["economics"]
    assert economics["recovered"]["minor_units"] > 0, (
        "the real economics-attachment function must report nonzero recovered value for "
        "a real, successful agentic markdown decision, not a fabricated one"
    )
    assert economics["recovered"]["minor_units"] == result["decision"]["expected_outcome"][
        "incremental_profit_minor_units"
    ]


def test_agentic_golden_cascade_threads_the_caller_audit_log_into_its_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every agentic cascade used to build its own throwaway AuditLog() internally, so no
    real tool call an agent made in production was ever recorded in the shared
    `/tools/platform/audit` trail. The cascade must use the caller-supplied audit log
    instead of silently discarding it."""
    from shelfwise_backend import agentic_cascade as agentic_cascade_module

    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))
    shared_audit = AuditLog()
    captured: dict[str, Any] = {}
    real_build_platform_tools = agentic_cascade_module.build_platform_tools

    def spying_build_platform_tools(**kwargs: Any):
        captured["audit"] = kwargs.get("audit")
        return real_build_platform_tools(**kwargs)

    monkeypatch.setattr(
        agentic_cascade_module, "build_platform_tools", spying_build_platform_tools
    )

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
        audit=shared_audit,
    )

    assert captured["audit"] is shared_audit


def test_critic_gate_overrides_an_executive_that_escalates_past_a_failed_critic() -> None:
    """The critic's verdict must be BINDING on routing, not advisory prose.

    The critic verdict reaches the executive only as text inside a prompt, and a
    hallucinating executive can answer "apply_markdown" even though the critic failed
    the work. Before 2026-07-15 the builders routed whatever the executive said, so the
    two agents could "talk past each other" and a critic-rejected HIGH-risk markdown
    still went forward for approval. The deterministic gate must force the safe action
    and put the override on the decision record.
    """
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    disagreeing_messages = [
        _tool_call_message("call_1", "get_stock", {"sku": sku}),
        _tool_call_message("call_2", "simulate_markdown", {"sku": sku, "discount_pct": 0.2}),
        _final_message(
            {
                "conclusion": (
                    f"{on_hand} units on hand but the simulated incremental profit of "
                    f"R{profit} does not clear the bar; the markdown is not justified."
                ),
                "confidence": 0.9,
                "critic_passed": False,
                "requires_human_review": True,
            }
        ),
        _final_message(
            {
                "conclusion": "Ship the markdown anyway, it feels right.",
                "confidence": 0.55,
                "recommended_action_type": "apply_markdown",
            }
        ),
    ]
    runtime = _FakeRuntime(disagreeing_messages)

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    decision = result["decision"]
    assert decision["action"]["type"] == "monitor", (
        "a failed critic verdict must force the safe action even when the executive "
        "answers with the escalating one"
    )
    assert decision["critic_verdict"] == "rejected"
    gate = decision["critic_gate"]
    assert gate == {
        "critic_passed": False,
        "executive_action_type": "apply_markdown",
        "override_applied": True,
    }


def test_critic_gate_lets_an_agreeing_executive_route_forward_untouched() -> None:
    """When both agents agree, the gate must be a no-op and say so on the record."""
    tools, decisions, memory, facts = _build_tools()
    sku, on_hand, profit = _hero_markdown_profit(facts)
    runtime = _FakeRuntime(_scripted_messages(sku, on_hand, profit))

    def factory() -> AgentOrchestrator:
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    result = run_golden_cascade_via_agents(
        event=None,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=factory,
    )

    decision = result["decision"]
    assert decision["action"]["type"] == "apply_markdown"
    assert decision["critic_gate"] == {
        "critic_passed": True,
        "executive_action_type": "apply_markdown",
        "override_applied": False,
    }
