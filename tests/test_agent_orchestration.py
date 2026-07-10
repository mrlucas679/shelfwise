from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from shelfwise_inference.orchestration import (
    AgentArchitecture,
    AgentOrchestrationError,
    AgentOrchestrator,
    ArchitectureMode,
    ExecutionMode,
    LiveInferenceRequiredError,
    ModelCall,
    RoleModelTarget,
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
    assert runtime.requests[0]["response_format"]["json_schema"]["strict"] is True
    tool_message = runtime.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert '"on_hand":12' in tool_message["content"]
    assert all(
        request["correlation_id"] == "corr-agent" for request in runtime.requests
    )


def test_agent_rejects_invalid_final_json_without_fallback() -> None:
    runtime = _FakeRuntime(
        [{"role": "assistant", "content": '{"risk":"high","action":4}'}]
    )
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
    assert len(runtime.requests) == 1


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
