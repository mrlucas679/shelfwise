from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from shelfwise_action import create_decision_store
from shelfwise_backend.chat import build_chat_reply_with_meta
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
    """Deterministic stand-in that answers immediately with no tool call, on a named model."""

    model: str
    mode: ExecutionMode = ExecutionMode.OFFLINE_TEST
    provider: str = "deterministic_fake"

    def __post_init__(self) -> None:
        self.architecture = AgentArchitecture(
            mode=ArchitectureMode.SHARED,
            default_target=RoleModelTarget("fake://runtime", self.model),
        )
        self.execution_mode = self.mode

    def complete(self, **kwargs: Any) -> ModelCall:
        return ModelCall(
            call_id="model_1",
            role=kwargs["role"],
            message={
                "role": "assistant",
                "content": json.dumps({"answer": "Stock looks fine right now."}),
            },
            provider=self.provider,
            model=self.model,
            endpoint="fake://runtime",
            used_network=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=3,
            correlation_id=kwargs["correlation_id"],
            finish_reason="stop",
        )


class _FakeConfig:
    api_key_present = True
    routine_model = "gemma-routine"
    strong_model = "gemma-strong"

    class provider:
        value = "fake_provider"


class _FakeInferenceClient:
    config = _FakeConfig()


def _run_chat_with_model(model: str) -> dict[str, Any]:
    decisions = create_decision_store()
    memory = create_learning_store()
    facts = WorldFactsProvider(InMemoryWorldSnapshotStore())
    tools: list[Any] = []
    runtime = _FakeRuntime(model=model)
    orchestrator = AgentOrchestrator(tools=tools, model_runtime=runtime)
    _, meta = build_chat_reply_with_meta(
        question="Is SKU-1 in stock?",
        state={},
        client=_FakeInferenceClient(),
        decisions=decisions,
        memory=memory,
        facts=facts,
        tenant_id="sa_retail_demo",
        orchestrator_factory=lambda: orchestrator,
    )
    return meta


def test_agentic_chat_reports_the_model_that_actually_answered_not_a_hardcoded_guess() -> None:
    """role="chat" can resolve to either tier depending on architecture - the reported
    `meta["model"]` must reflect whichever model actually produced the answer, not an
    assumption made before the run even started."""
    routine_meta = _run_chat_with_model("gemma-routine-tier")
    assert routine_meta["model"] == "gemma-routine-tier"
    assert routine_meta["answer_source"] == "model"

    strong_meta = _run_chat_with_model("gemma-strong-tier")
    assert strong_meta["model"] == "gemma-strong-tier"
