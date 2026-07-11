from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from shelfwise_inference.client import (
    InferenceError,
    InferenceResult,
    OpenAICompatibleInferenceClient,
    RunRecorder,
)
from shelfwise_inference.config import STRONG_AGENT_NAMES, InferenceConfig, load_inference_config
from shelfwise_inference.orchestration import (
    AgentArchitecture,
    AgentOrchestrationError,
    ArchitectureMode,
    ExecutionMode,
    LiveInferenceRequiredError,
    ModelCall,
    RoleModelTarget,
    enforce_execution_mode,
)

_AGENT_ROLES = (
    "inventory",
    "demand",
    "expiry",
    "opportunity",
    "simulation",
    "critic",
    "executive",
    "orchestrator",
)


class ChatCompletionsClient(Protocol):
    """Structural client contract used by the real and deterministic fake providers."""

    def chat_completions(self, **kwargs: Any) -> InferenceResult:
        """Submit one generic OpenAI-compatible chat request."""
        ...


class OpenAIModelRuntime:
    """Route one agent call to a configured OpenAI-compatible endpoint/model pair."""

    def __init__(
        self,
        *,
        architecture: AgentArchitecture,
        execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
        client: ChatCompletionsClient | None = None,
        recorder: RunRecorder | None = None,
    ) -> None:
        if client is not None and recorder is not None:
            raise ValueError("recorder cannot be supplied with an injected model client")
        self.architecture = architecture
        self.execution_mode = ExecutionMode(execution_mode)
        self._client = (
            client if client is not None else OpenAICompatibleInferenceClient(recorder=recorder)
        )

    def complete(
        self,
        *,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None,
        response_format: dict[str, Any],
        correlation_id: str,
        temperature: float,
        max_tokens: int,
        tenant_id: str,
        schema_version: str,
    ) -> ModelCall:
        """Submit one routed request and normalize its evidence for orchestration."""
        target = self.architecture.target_for(role)
        try:
            result = self._client.chat_completions(
                agent=role,
                messages=messages,
                tools=tools or None,
                tool_choice=tool_choice,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                schema_version=schema_version,
                model=target.model,
                base_url=target.endpoint,
            )
        except InferenceError as exc:
            if self.execution_mode is ExecutionMode.LIVE_REQUIRED:
                raise LiveInferenceRequiredError(
                    f"live_required model call to role {role!r} failed: {exc}"
                ) from exc
            raise AgentOrchestrationError(f"model call to role {role!r} failed: {exc}") from exc
        if not isinstance(result, InferenceResult):
            raise TypeError("model client returned an invalid inference result")
        message = result.message or {"role": "assistant", "content": result.content}
        model_call = ModelCall(
            call_id=result.run_id or f"model_{uuid4().hex[:12]}",
            role=role,
            message=message,
            provider=result.provider,
            model=result.model,
            endpoint=target.endpoint,
            used_network=result.used_network,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            correlation_id=result.correlation_id or correlation_id,
            finish_reason=result.finish_reason,
            status=result.status,
            fallback=result.fallback or "fallback" in result.status.lower(),
        )
        enforce_execution_mode(model_call, self.execution_mode)
        return model_call


def architecture_from_inference_config(
    config: InferenceConfig | None = None,
    *,
    mode: ArchitectureMode = ArchitectureMode.HYBRID,
) -> AgentArchitecture:
    """Translate existing routine/strong settings into role routing only."""
    resolved = config or load_inference_config()
    architecture_mode = ArchitectureMode(mode)
    routine = RoleModelTarget(
        endpoint=resolved.base_url_for_agent("inventory"), model=resolved.routine_model
    )
    strong = RoleModelTarget(
        endpoint=resolved.base_url_for_agent("executive"), model=resolved.strong_model
    )
    if architecture_mode is ArchitectureMode.SHARED:
        return AgentArchitecture(mode=architecture_mode, default_target=routine)
    role_targets = {
        role: strong if role in STRONG_AGENT_NAMES else routine for role in _AGENT_ROLES
    }
    if architecture_mode is ArchitectureMode.PER_AGENT:
        return AgentArchitecture(mode=architecture_mode, role_targets=role_targets)
    if architecture_mode is ArchitectureMode.HYBRID:
        strong_targets = {role: strong for role in _AGENT_ROLES if role in STRONG_AGENT_NAMES}
        return AgentArchitecture(
            mode=architecture_mode,
            default_target=routine,
            role_targets=strong_targets,
        )
    return AgentArchitecture(mode=architecture_mode, role_targets=role_targets)
