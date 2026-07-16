from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_inference import orchestration
from shelfwise_inference.orchestration import (
    AgentArchitecture,
    AgentOrchestrationError,
    AgentOrchestrator,
    ArchitectureMode,
    CascadeDeadlineExceeded,
    ExecutionMode,
    LiveInferenceRequiredError,
    ModelCall,
    RoleModelTarget,
    _final_response_format,
)
from shelfwise_inference.tool_calling import FinalAnswerValidationError


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    read_only: bool
    fn: Any


async def _get_stock(sku: str) -> dict[str, Any]:
    return {"sku": sku, "on_hand": 12, "source": "deterministic_fake"}


class _FakeRuntime:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: ExecutionMode = ExecutionMode.OFFLINE_TEST,
        used_network: bool = False,
        provider: str = "deterministic_fake",
        fallback: bool = False,
    ) -> None:
        self.architecture = AgentArchitecture(
            mode=ArchitectureMode.SHARED,
            default_target=RoleModelTarget("fake://runtime", "gemma-fake"),
        )
        self.execution_mode = mode
        self._messages = messages
        self._used_network = used_network
        self._provider = provider
        self._fallback = fallback
        self.requests: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> ModelCall:
        self.requests.append(deepcopy(kwargs))
        index = len(self.requests) - 1
        return ModelCall(
            call_id=f"model_{index + 1}",
            role=kwargs["role"],
            message=self._messages[index],
            provider=self._provider,
            model="gemma-fake",
            endpoint="fake://runtime",
            used_network=self._used_network,
            input_tokens=10 + index,
            output_tokens=3 + index,
            latency_ms=2 + index,
            correlation_id=kwargs["correlation_id"],
            finish_reason="tool_calls" if index == 0 else "stop",
            fallback=self._fallback,
        )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "risk": {"type": "string", "enum": ["low", "high"]},
            "action": {"type": "string"},
        },
        "required": ["risk", "action"],
        "additionalProperties": False,
    }


def test_strong_gemma_uses_native_json_schema_while_routine_stays_text() -> None:
    schema = _schema()

    strong = _final_response_format(
        model="google/gemma-4-31B-it",
        schema_name="agent_answer",
        schema=schema,
    )

    assert strong["type"] == "json_schema"
    assert strong["json_schema"]["strict"] is True
    assert strong["json_schema"]["schema"] == schema
    assert _final_response_format(
        model="google/gemma-4-E4B-it",
        schema_name="agent_answer",
        schema=schema,
    ) == {"type": "text"}


def test_agent_executes_tool_feeds_result_back_and_returns_valid_json() -> None:
    runtime = _FakeRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {
                            "name": "get_stock",
                            "arguments": '{"sku":"4011"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ]
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="inventory",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-agent",
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert result.correlation_id == "corr-agent"
    assert len(result.model_calls) == 2
    assert len(result.tool_calls) == 1
    assert result.to_dict()["usage"] == {
        "input_tokens": 21,
        "output_tokens": 7,
        "total_tokens": 28,
    }
    assert runtime.requests[0]["tools"][0]["function"]["name"] == "get_stock"
    # Strict json_schema-constrained decoding was found (live, against Gemma-4-E4B-it/vLLM)
    # to cause a reproducible non-terminating whitespace loop; the schema is instead spelled
    # out as a system reminder and enforced by post-hoc parse_and_validate_json_answer.
    assert runtime.requests[0]["response_format"] == {"type": "text"}
    schema_reminder = runtime.requests[0]["messages"][-1]
    assert schema_reminder["role"] == "system"
    assert '"risk"' in schema_reminder["content"]
    tool_message = runtime.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert '"on_hand":12' in tool_message["content"]
    assert all(
        request["correlation_id"] == "corr-agent" for request in runtime.requests
    )


def test_agent_rejects_invalid_final_json_without_fallback() -> None:
    """Invalid JSON gets a bounded number of retries (a real, live decoding stall found
    against Gemma-4-E4B-it/vLLM was intermittent even at temperature=0), but still hard-fails
    - never silently falls back - once retries are exhausted.
    """
    bad_message = {"role": "assistant", "content": '{"risk":"high","action":4}'}
    runtime = _FakeRuntime([bad_message, bad_message, bad_message])
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    with pytest.raises(FinalAnswerValidationError, match="wrong JSON type"):
        asyncio.run(
            orchestrator.run(
                role="inventory",
                system="Assess stock risk.",
                user="Check SKU 4011.",
                final_schema=_schema(),
            )
        )
    assert len(runtime.requests) == 3


def test_agent_recovers_after_one_invalid_final_json_retry() -> None:
    bad_message = {"role": "assistant", "content": '{"risk":"high","action":4}'}
    good_message = {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'}
    runtime = _FakeRuntime([bad_message, good_message])
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="inventory",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert len(runtime.requests) == 2


@pytest.mark.parametrize(
    ("used_network", "provider", "fallback"),
    [
        (False, "vllm_mi300x", False),
        (True, "fallback", True),
    ],
)
def test_live_required_rejects_non_network_and_fallback_results(
    used_network: bool,
    provider: str,
    fallback: bool,
) -> None:
    runtime = _FakeRuntime(
        [{"role": "assistant", "content": '{"risk":"low","action":"none"}'}],
        mode=ExecutionMode.LIVE_REQUIRED,
        used_network=used_network,
        provider=provider,
        fallback=fallback,
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    with pytest.raises(LiveInferenceRequiredError, match="live_required rejected"):
        asyncio.run(
            orchestrator.run(
                role="inventory",
                system="Assess stock risk.",
                user="Check SKU 4011.",
                final_schema=_schema(),
            )
        )


def test_architecture_modes_only_resolve_role_targets() -> None:
    routine = RoleModelTarget("https://shared.example/v1", "gemma-routine")
    strong = RoleModelTarget("https://strong.example/v1", "gemma-strong")
    shared = AgentArchitecture(ArchitectureMode.SHARED, default_target=routine)
    per_agent = AgentArchitecture(
        ArchitectureMode.PER_AGENT,
        role_targets={"inventory": routine, "critic": strong},
    )
    hybrid = AgentArchitecture(
        ArchitectureMode.HYBRID,
        default_target=routine,
        role_targets={"critic": strong},
    )
    replicated = AgentArchitecture(
        ArchitectureMode.REPLICATED,
        default_target=routine,
        role_targets={"inventory": RoleModelTarget("https://replica.example/v1", "gemma-routine")},
    )

    assert shared.target_for("critic") == routine
    assert per_agent.target_for("critic") == strong
    assert hybrid.target_for("inventory") == routine
    assert hybrid.target_for("critic") == strong
    assert replicated.target_for("inventory").endpoint == "https://replica.example/v1"
    with pytest.raises(AgentOrchestrationError, match="no model target"):
        per_agent.target_for("executive")


def test_require_tool_call_first_forces_tool_choice_required_on_opening_call() -> None:
    """Some providers' "auto" tool choice skips straight to a final answer instead of
    gathering evidence first - discovered against a real live Gemma-4/vLLM endpoint,
    where it produced a degenerate, schema-violating answer. require_tool_call_first
    must force tool_choice="required" on the opening request only.
    """
    runtime = _FakeRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ]
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="critic",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-required",
            require_tool_call_first=True,
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert runtime.requests[0]["tool_choice"] == "required"
    assert runtime.requests[1]["tool_choice"] == "auto"


def test_require_tool_call_first_rejects_provider_that_ignores_requirement() -> None:
    """A model that ignores the forced opening tool call gets a bounded number of retries
    (Gemma occasionally does this nondeterministically), but still hard-fails - never falls
    back to the ungrounded direct answer - once the retries are exhausted.
    """
    ignored = {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'}
    runtime = _FakeRuntime([ignored, ignored, ignored])
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    with pytest.raises(AgentOrchestrationError, match="ignored the required opening tool"):
        asyncio.run(
            orchestrator.run(
                role="critic",
                system="Assess stock risk.",
                user="Check SKU 4011.",
                final_schema=_schema(),
                correlation_id="corr-required-ignored",
                require_tool_call_first=True,
            )
        )
    assert len(runtime.requests) == 3, "expected the opening call plus two forced retries"
    assert all(request["tool_choice"] == "required" for request in runtime.requests), (
        "every retry must re-force the opening tool call, not fall back to auto"
    )


def test_require_tool_call_first_recovers_when_model_complies_on_retry() -> None:
    """If the model ignores the forced opening call once but complies on the retry, the
    cascade proceeds normally - the non-compliance is recovered, not fatal.
    """
    runtime = _FakeRuntime(
        [
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ]
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="critic",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-required-recovered",
            require_tool_call_first=True,
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert len(result.tool_calls) == 1
    assert runtime.requests[0]["tool_choice"] == "required"
    assert runtime.requests[1]["tool_choice"] == "required"


def test_strong_tier_forced_tool_call_never_competes_with_guided_json() -> None:
    """Root-caused live on the 2026-07-15 hot soak: the 31B strong tier's native
    `json_schema` guided decoding, combined with `tool_choice="required"` on the same
    turn, made the model emit schema-shaped junk (max_tokens of empty content) instead of
    the forced tool call - a 100% reproducible failure for the expiry-risk and cold-chain
    critics, not a flaky one. The forced-tool-call turn must always request plain text so
    the tool call can win cleanly; only the final-answer turn may request the strict
    json_schema format.
    """
    runtime = _FakeRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ]
    )
    runtime.architecture = AgentArchitecture(
        mode=ArchitectureMode.SHARED,
        default_target=RoleModelTarget("fake://strong", "google/gemma-4-31B-it"),
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="critic",
            system="Assess expiry risk.",
            user="Assess batch B1 for SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-strong-forced-tool",
            require_tool_call_first=True,
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    opening_call, answer_call = runtime.requests
    assert opening_call["tool_choice"] == "required"
    assert opening_call["response_format"] == {"type": "text"}, (
        "forcing a tool call must never also request guided-JSON decoding on that turn"
    )
    assert answer_call["response_format"]["type"] == "json_schema", (
        "the final-answer turn on the strong tier must still get native schema constraints"
    )


def test_required_named_tool_retries_malformed_opening_arguments() -> None:
    runtime = _FakeRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_broken",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":'},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ]
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="critic",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-malformed-retry",
            require_tool_call_first=True,
            required_tool_names=("get_stock",),
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert len(result.tool_calls) == 1
    assert all(
        request["tool_choice"]
        == {"type": "function", "function": {"name": "get_stock"}}
        for request in runtime.requests[:2]
    )
    correction = runtime.requests[1]["messages"][-1]
    assert correction["role"] == "system"
    assert "invalid JSON arguments" in correction["content"]


def test_tool_choice_defaults_to_auto_without_require_tool_call_first() -> None:
    runtime = _FakeRuntime([{"role": "assistant", "content": '{"risk":"low","action":"noop"}'}])
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    asyncio.run(
        orchestrator.run(
            role="inventory",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-auto",
        )
    )

    assert runtime.requests[0]["tool_choice"] == "auto"


class _ClockAdvancingRuntime:
    """A fake runtime whose every call consumes a fixed amount of simulated wall-clock time.

    Used to prove `deadline` stops the loop before it starts a model call it cannot finish,
    without an actual sleep - `orchestration.monotonic` is monkeypatched to read `clock["t"]`,
    and each `complete()` call advances it by `seconds_per_call`.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        clock: dict[str, float],
        seconds_per_call: float,
    ) -> None:
        self.architecture = AgentArchitecture(
            mode=ArchitectureMode.SHARED,
            default_target=RoleModelTarget("fake://runtime", "gemma-fake"),
        )
        self.execution_mode = ExecutionMode.OFFLINE_TEST
        self._messages = messages
        self._clock = clock
        self._seconds_per_call = seconds_per_call
        self.requests: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> ModelCall:
        self.requests.append(deepcopy(kwargs))
        index = len(self.requests) - 1
        self._clock["t"] += self._seconds_per_call
        return ModelCall(
            call_id=f"model_{index + 1}",
            role=kwargs["role"],
            message=self._messages[index],
            provider="deterministic_fake",
            model="gemma-fake",
            endpoint="fake://runtime",
            used_network=False,
            input_tokens=10,
            output_tokens=3,
            latency_ms=2,
            correlation_id=kwargs["correlation_id"],
            finish_reason="tool_calls" if index == 0 else "stop",
        )


def test_deadline_stops_the_loop_before_a_call_it_cannot_finish(monkeypatch) -> None:
    """Each simulated call costs 10s. Starting from clock=1000 with a deadline 12s out, the
    loop may safely start call 1 (0s elapsed, 12s left), but must refuse call 2 (10s elapsed,
    only 2s left - below `_MIN_CALL_BUDGET_S`), raising CascadeDeadlineExceeded after exactly
    one completed model call instead of burning GPU time on a request nobody awaits anymore.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(orchestration, "monotonic", lambda: clock["t"])
    runtime = _ClockAdvancingRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ],
        clock=clock,
        seconds_per_call=10.0,
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    with pytest.raises(CascadeDeadlineExceeded) as excinfo:
        asyncio.run(
            orchestrator.run(
                role="critic",
                system="Assess stock risk.",
                user="Check SKU 4011.",
                final_schema=_schema(),
                deadline=clock["t"] + 12.0,
            )
        )

    assert excinfo.value.completed_model_calls == 1
    assert len(runtime.requests) == 1


def test_no_deadline_leaves_multi_call_behavior_unchanged(monkeypatch) -> None:
    """Omitting `deadline` (the default) must not change existing cascade behavior - the
    loop runs to completion across as many calls as it needs regardless of elapsed time.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(orchestration, "monotonic", lambda: clock["t"])
    runtime = _ClockAdvancingRuntime(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stock",
                        "type": "function",
                        "function": {"name": "get_stock", "arguments": '{"sku":"4011"}'},
                    }
                ],
            },
            {"role": "assistant", "content": '{"risk":"high","action":"monitor"}'},
        ],
        clock=clock,
        seconds_per_call=10.0,
    )
    orchestrator = AgentOrchestrator(
        tools=[_Tool("get_stock", "Read stock.", True, _get_stock)],
        model_runtime=runtime,
    )

    result = asyncio.run(
        orchestrator.run(
            role="critic",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
        )
    )

    assert result.answer == {"risk": "high", "action": "monitor"}
    assert len(runtime.requests) == 2


def test_tool_choice_is_none_when_no_tools_registered() -> None:
    runtime = _FakeRuntime([{"role": "assistant", "content": '{"risk":"low","action":"noop"}'}])
    orchestrator = AgentOrchestrator(tools=[], model_runtime=runtime)

    asyncio.run(
        orchestrator.run(
            role="inventory",
            system="Assess stock risk.",
            user="Check SKU 4011.",
            final_schema=_schema(),
            correlation_id="corr-none",
            require_tool_call_first=True,
        )
    )

    assert runtime.requests[0]["tool_choice"] is None
