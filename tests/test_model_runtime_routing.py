from time import monotonic
from typing import Any

from shelfwise_backend.tools.model_runtime import (
    OpenAIModelRuntime,
    architecture_from_inference_config,
)
from shelfwise_inference.client import InferenceResult
from shelfwise_inference.config import InferenceConfig, ProviderKind
from shelfwise_inference.orchestration import (
    AgentArchitecture,
    ArchitectureMode,
    ExecutionMode,
    RoleModelTarget,
)


def test_hybrid_runtime_routes_tiers_to_independent_endpoints() -> None:
    config = InferenceConfig(
        provider=ProviderKind.VLLM_MI300X,
        base_url="https://routine.example/v1",
        routine_model="google/gemma-4-E4B-it",
        strong_model="google/gemma-4-31B-it",
        api_key="common-key",
        api_key_present=True,
        routine_base_url="https://routine.example/v1",
        strong_base_url="https://strong.example/v1",
    )

    architecture = architecture_from_inference_config(config)

    assert architecture.target_for("inventory").endpoint == "https://routine.example/v1"
    assert architecture.target_for("inventory").model == "google/gemma-4-E4B-it"
    assert architecture.target_for("critic").endpoint == "https://strong.example/v1"
    assert architecture.target_for("critic").model == "google/gemma-4-31B-it"


class _FakeChatClient:
    """Records every kwarg `chat_completions` is called with, including the timeout."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat_completions(self, **kwargs: Any) -> InferenceResult:
        self.calls.append(kwargs)
        return InferenceResult(
            provider="vllm_mi300x",
            model=str(kwargs["model"]),
            content='{"risk":"low"}',
            used_network=True,
            message={"role": "assistant", "content": '{"risk":"low"}'},
        )


def _runtime(client: _FakeChatClient, *, timeout_seconds: int = 25) -> OpenAIModelRuntime:
    return OpenAIModelRuntime(
        architecture=AgentArchitecture(
            mode=ArchitectureMode.SHARED,
            default_target=RoleModelTarget("https://strong.example/v1", "google/gemma-4-31B-it"),
        ),
        execution_mode=ExecutionMode.LIVE_REQUIRED,
        client=client,
        config=InferenceConfig(
            provider=ProviderKind.VLLM_MI300X,
            base_url="https://strong.example/v1",
            routine_model="google/gemma-4-E4B-it",
            strong_model="google/gemma-4-31B-it",
            api_key="k",
            api_key_present=True,
            timeout_seconds=timeout_seconds,
        ),
    )


def _complete(runtime: OpenAIModelRuntime, **overrides: Any) -> None:
    runtime.complete(
        **{
            "role": "critic",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "tool_choice": None,
            "response_format": {"type": "text"},
            "correlation_id": "corr-1",
            "temperature": 0.0,
            "max_tokens": 200,
            "tenant_id": "default",
            "schema_version": "v1",
            **overrides,
        }
    )


def test_bounded_timeout_never_exceeds_the_remaining_deadline_budget() -> None:
    """A near-expired deadline must bound the outbound HTTP timeout to what's actually left
    (floored at 1.0s), not the full configured ceiling - otherwise the call keeps running on
    the GPU well past the point the caller already gave up (the zombie-inference defect).
    """
    client = _FakeChatClient()
    runtime = _runtime(client, timeout_seconds=25)

    _complete(runtime, deadline=monotonic() + 2.0)

    assert client.calls[0]["timeout_seconds"] <= 2.0


def test_no_deadline_uses_the_configured_timeout_ceiling_unchanged() -> None:
    """Omitting `deadline` (the default everywhere outside the agentic demo routes) must not
    change existing outbound timeout behavior."""
    client = _FakeChatClient()
    runtime = _runtime(client, timeout_seconds=25)

    _complete(runtime)

    assert client.calls[0]["timeout_seconds"] == 25.0
