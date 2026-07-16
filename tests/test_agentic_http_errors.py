from __future__ import annotations

from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.agentic_cascade import AgenticCascadeDeadlineError, AgenticCascadeError


def test_agentic_provider_details_are_logged_not_returned_to_clients(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AgenticCascadeError("provider https://secret.example/v1 failed with token=hidden")

    monkeypatch.setattr(app_module, "run_golden_cascade_via_agents", fail)

    response = TestClient(app_module.app).post("/scenarios/golden/agentic")

    assert response.status_code == 503
    assert response.json()["detail"] == "Live agentic inference is unavailable"
    assert "secret.example" not in response.text
    assert "token" not in response.text


def test_agentic_deadline_exceeded_returns_structured_503_with_progress(monkeypatch) -> None:
    """A cascade that stops itself before the response deadline must tell the caller how
    far it got, instead of returning the same opaque unavailability message as a genuine
    provider failure (the 2026-07-14 forensic audit found no way to distinguish the two).
    """

    def deadline_hit(*args, **kwargs):
        raise AgenticCascadeDeadlineError(
            "live agentic cold-chain cascade could not finish inside the response deadline",
            completed_model_calls=1,
            elapsed_ms=9500,
        )

    monkeypatch.setattr(app_module, "run_cold_chain_cascade_via_agents", deadline_hit)

    response = TestClient(app_module.app).post("/scenarios/cold-chain/agentic")

    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["detail"] == "cascade could not finish inside the response deadline"
    assert body["completed_model_calls"] == 1
    assert body["elapsed_ms"] == 9500


def test_a_genuinely_misbehaving_model_maps_to_the_same_sanitized_503(monkeypatch) -> None:
    """Provoke the failure inside the REAL cascade instead of fabricating the exception.

    The two tests above monkeypatch the cascade function itself to raise, which proves
    only the route's mapping layer. Here a scripted runtime keeps answering directly
    despite the forced tool call - production Gemma's observed misbehavior - so the
    real orchestrator retry-bounds logic raises the real ToolCallingError, the real
    cascade wraps it in AgenticCascadeError, and the route must still return the same
    sanitized 503 with no provider internals in the body.
    """
    from copy import deepcopy
    from typing import Any

    from shelfwise_backend import agentic_cascade as cascade_module
    from shelfwise_inference.orchestration import (
        AgentArchitecture,
        AgentOrchestrator,
        ArchitectureMode,
        ExecutionMode,
        ModelCall,
        RoleModelTarget,
    )

    class _StubbornlyDirectRuntime:
        """Never calls a tool no matter how many retries the orchestrator grants."""

        provider = "deterministic_fake"
        used_network = False
        fallback = False

        def __init__(self) -> None:
            self.architecture = AgentArchitecture(
                mode=ArchitectureMode.SHARED,
                default_target=RoleModelTarget("fake://runtime", "gemma-fake"),
            )
            self.execution_mode = ExecutionMode.OFFLINE_TEST
            self.calls = 0

        def complete(self, **kwargs: Any) -> ModelCall:
            self.calls += 1
            return ModelCall(
                call_id=f"model_{self.calls}",
                role=kwargs["role"],
                message=deepcopy(
                    {"role": "assistant", "content": '{"answer": "I refuse to use tools"}'}
                ),
                provider=self.provider,
                model="gemma-fake",
                endpoint="fake://runtime",
                used_network=False,
                input_tokens=10,
                output_tokens=5,
                latency_ms=3,
                correlation_id=kwargs["correlation_id"],
                finish_reason="stop",
                fallback=False,
            )

    runtime = _StubbornlyDirectRuntime()

    def fake_default_orchestrator(*, tools, execution_mode, recorder=None):
        return AgentOrchestrator(tools=tools, model_runtime=runtime)

    monkeypatch.setattr(cascade_module, "_default_orchestrator", fake_default_orchestrator)

    response = TestClient(app_module.app).post(
        "/scenarios/golden/agentic", params={"live_required": "false"}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Live agentic inference is unavailable"
    assert "fake://runtime" not in response.text, "endpoint internals must never leak"
    assert runtime.calls >= 1, "the real orchestrator must actually have driven the runtime"
