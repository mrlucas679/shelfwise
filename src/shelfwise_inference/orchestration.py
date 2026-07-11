from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from inspect import isawaitable
from time import perf_counter
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .tool_calling import (
    FinalAnswerValidationError,
    PlatformToolLike,
    PlatformToolRegistry,
    ToolExecution,
    parse_and_validate_json_answer,
    parse_tool_calls,
)

_ROLE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OK_STATUSES = {"ok", "success"}
_OFFLINE_PROVIDERS = {"offline", "fallback"}
_MAX_FINAL_ANSWER_RETRIES = 2
# Strict json_schema-constrained decoding (response_format={"type": "json_schema", ...})
# was found live against Gemma-4-E4B-it/vLLM to cause a reproducible, non-token-budget-
# fixable failure: after closing the first string value the guided-decoding grammar falls
# into an indentation-whitespace loop and never emits the remaining required fields, even
# with max_tokens raised from 800 to 4000 (confirmed via raw HTTP probe - completion_tokens
# matched the requested cap exactly both times, so it never "ran out of room", it just never
# stops). Plain-text generation with the schema spelled out in a system reminder, validated
# after the fact by parse_and_validate_json_answer (already required regardless), avoided
# the loop entirely and returned a complete, valid answer in ~80 tokens. This is enforcement-
# after-generation rather than during it, but the "no silent fallback" contract is unchanged:
# an invalid answer still hard-fails (after the existing bounded retry).
_TEXT_RESPONSE_FORMAT: dict[str, Any] = {"type": "text"}


class ExecutionMode(StrEnum):
    """Controls whether deterministic non-network providers are permitted."""

    OFFLINE_TEST = "offline_test"
    LIVE_REQUIRED = "live_required"


class ArchitectureMode(StrEnum):
    """Names role-routing configurations without implying process topology."""

    SHARED = "shared"
    PER_AGENT = "per_agent"
    HYBRID = "hybrid"
    REPLICATED = "replicated"


class AgentOrchestrationError(RuntimeError):
    """Raised when a model/tool loop violates its explicit runtime contract."""


class LiveInferenceRequiredError(AgentOrchestrationError):
    """Raised when live-required execution observes offline or fallback output."""


@dataclass(frozen=True, slots=True)
class RoleModelTarget:
    """Endpoint and model assigned to one role by architecture configuration."""

    endpoint: str
    model: str

    def __post_init__(self) -> None:
        """Reject incomplete model targets before any provider request is made."""
        if not self.model.strip():
            raise ValueError("role model target requires a model name")


@dataclass(frozen=True, slots=True)
class AgentArchitecture:
    """Resolve roles to endpoint/model pairs for one named architecture mode."""

    mode: ArchitectureMode
    default_target: RoleModelTarget | None = None
    role_targets: Mapping[str, RoleModelTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize immutable role mappings and enforce mode-specific requirements."""
        mode = ArchitectureMode(self.mode)
        normalized: dict[str, RoleModelTarget] = {}
        for role, target in self.role_targets.items():
            normalized_role = _normalized_role(role)
            if normalized_role in normalized:
                raise ValueError(f"duplicate architecture role: {normalized_role}")
            normalized[normalized_role] = target
        if mode is ArchitectureMode.SHARED and self.default_target is None:
            raise ValueError("shared architecture requires a default target")
        if mode is ArchitectureMode.SHARED and normalized:
            raise ValueError("shared architecture cannot define role overrides")
        if mode is ArchitectureMode.PER_AGENT and not normalized:
            raise ValueError("per_agent architecture requires role targets")
        if mode is ArchitectureMode.PER_AGENT and self.default_target is not None:
            raise ValueError("per_agent architecture cannot define a default target")
        if mode is ArchitectureMode.HYBRID and self.default_target is None:
            raise ValueError("hybrid architecture requires a default target")
        if mode is ArchitectureMode.REPLICATED and not normalized and self.default_target is None:
            raise ValueError("replicated architecture requires at least one target")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "role_targets", MappingProxyType(normalized))

    def target_for(self, role: str) -> RoleModelTarget:
        """Resolve a role without creating workers, replicas, or provider fallbacks."""
        normalized_role = _normalized_role(role)
        if self.mode is ArchitectureMode.SHARED:
            if self.default_target is None:
                raise AgentOrchestrationError("shared architecture has no default target")
            return self.default_target
        target = self.role_targets.get(normalized_role)
        if target is not None:
            return target
        if (
            self.mode in {ArchitectureMode.HYBRID, ArchitectureMode.REPLICATED}
            and self.default_target is not None
        ):
            return self.default_target
        raise AgentOrchestrationError(f"no model target configured for role: {normalized_role}")

    def to_public_dict(self) -> dict[str, Any]:
        """Expose secret-safe endpoint hosts and role model assignments."""
        return {
            "mode": self.mode.value,
            "default_target": _target_public_dict(self.default_target),
            "role_targets": {
                role: _target_public_dict(target) for role, target in self.role_targets.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ModelCall:
    """Normalized provider response and per-call observability metadata."""

    call_id: str
    role: str
    message: Mapping[str, Any]
    provider: str
    model: str
    endpoint: str
    used_network: bool
    input_tokens: int
    output_tokens: int
    latency_ms: int
    correlation_id: str
    finish_reason: str = ""
    status: str = "ok"
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return trace metadata without leaking prompts, answers, or endpoint secrets."""
        return {
            "call_id": self.call_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "endpoint_host": _endpoint_host(self.endpoint),
            "used_network": self.used_network,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
            "latency_ms": self.latency_ms,
            "correlation_id": self.correlation_id,
            "finish_reason": self.finish_reason,
            "status": self.status,
            "fallback": self.fallback,
        }


class ModelRuntime(Protocol):
    """Runtime contract consumed by the provider-independent agent loop."""

    architecture: AgentArchitecture
    execution_mode: ExecutionMode

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
    ) -> ModelCall | Awaitable[ModelCall]:
        """Return one normalized OpenAI-compatible model response."""
        ...


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Schema-valid answer plus complete model and tool trace metadata."""

    role: str
    answer: Any
    correlation_id: str
    execution_mode: ExecutionMode
    architecture_mode: ArchitectureMode
    model_calls: tuple[ModelCall, ...]
    tool_calls: tuple[ToolExecution, ...]
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer, call counts, timing, usage, and correlations."""
        input_tokens = sum(call.input_tokens for call in self.model_calls)
        output_tokens = sum(call.output_tokens for call in self.model_calls)
        return {
            "role": self.role,
            "answer": self.answer,
            "correlation_id": self.correlation_id,
            "execution_mode": self.execution_mode.value,
            "architecture_mode": self.architecture_mode.value,
            "latency_ms": self.latency_ms,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "model_call_count": len(self.model_calls),
            "tool_call_count": len(self.tool_calls),
            "model_calls": [call.to_dict() for call in self.model_calls],
            "tool_calls": [call.to_dict() for call in self.tool_calls],
        }


class AgentOrchestrator:
    """Run a bounded Gemma tool loop and accept only schema-valid final JSON."""

    def __init__(
        self,
        *,
        tools: Sequence[PlatformToolLike],
        model_runtime: ModelRuntime,
        execution_mode: ExecutionMode | None = None,
        max_model_calls: int = 8,
    ) -> None:
        if max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")
        self._registry = PlatformToolRegistry(tools)
        self._runtime = model_runtime
        runtime_mode = ExecutionMode(model_runtime.execution_mode)
        self._execution_mode = ExecutionMode(execution_mode or runtime_mode)
        if execution_mode is not None and self._execution_mode != runtime_mode:
            raise ValueError("orchestrator and model runtime execution modes must match")
        self._max_model_calls = max_model_calls

    async def run(
        self,
        *,
        role: str,
        system: str,
        user: str,
        final_schema: Mapping[str, Any],
        final_schema_name: str | None = None,
        correlation_id: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        tenant_id: str = "default",
        require_tool_call_first: bool = False,
    ) -> AgentRunResult:
        """Run from a system/user prompt and return one validated agent answer."""
        guarded_system = (
            f"{system.rstrip()}\n\n"
            "Use only the supplied tools. Never invent tool results. The tools are your "
            "calculator: any number in your conclusion must be one the tools actually "
            "returned, not a guess or a paraphrase. Explain your reasoning by citing the "
            "specific figures you computed, not just a bare verdict. When evidence "
            "gathering is complete, return only JSON matching the required response schema."
        )
        messages = [
            {"role": "system", "content": guarded_system},
            {"role": "user", "content": user},
        ]
        return await self.run_messages(
            role=role,
            messages=messages,
            final_schema=final_schema,
            final_schema_name=final_schema_name,
            correlation_id=correlation_id,
            temperature=temperature,
            max_tokens=max_tokens,
            tenant_id=tenant_id,
            require_tool_call_first=require_tool_call_first,
        )

    async def run_messages(
        self,
        *,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        final_schema: Mapping[str, Any],
        final_schema_name: str | None = None,
        correlation_id: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        tenant_id: str = "default",
        require_tool_call_first: bool = False,
    ) -> AgentRunResult:
        """Run a bounded tool loop from pre-built OpenAI-compatible messages.

        require_tool_call_first forces tool_choice="required" on the opening request when
        tools are registered. Some providers' "auto" tool choice will, under certain prompts,
        skip straight to a (often degenerate, schema-violating) final answer instead of
        gathering evidence first - forcing the first call closes that gap.
        """
        normalized_role = _normalized_role(role)
        effective_correlation_id = correlation_id or f"agent_{uuid4().hex[:16]}"
        schema_name = final_schema_name or f"{normalized_role}_answer"
        response_format = _TEXT_RESPONSE_FORMAT
        openai_tools = self._registry.openai_tools()
        conversation = [dict(message) for message in messages]
        conversation.append(
            {
                "role": "system",
                "content": (
                    "Once you are done gathering evidence, your final response must be "
                    "exactly one JSON object matching this schema - no markdown code "
                    "fences, no explanation before or after it:\n"
                    f"{json.dumps(final_schema, sort_keys=True)}"
                ),
            }
        )
        model_calls: list[ModelCall] = []
        tool_executions: list[ToolExecution] = []
        seen_call_ids: set[str] = set()
        started = perf_counter()
        final_answer_retries = 0

        for call_index in range(self._max_model_calls):
            tool_choice: str | dict[str, Any] | None
            if not openai_tools:
                tool_choice = None
            elif require_tool_call_first and call_index == 0:
                tool_choice = "required"
            else:
                tool_choice = "auto"
            result = self._runtime.complete(
                role=normalized_role,
                messages=[dict(message) for message in conversation],
                tools=deepcopy_tools(openai_tools),
                tool_choice=tool_choice,
                response_format=response_format,
                correlation_id=effective_correlation_id,
                temperature=temperature,
                max_tokens=max_tokens,
                tenant_id=tenant_id,
                schema_version=schema_name,
            )
            model_call = await result if isawaitable(result) else result
            if not isinstance(model_call, ModelCall):
                raise AgentOrchestrationError("model runtime returned an invalid result")
            if not model_call.correlation_id:
                model_call = replace(model_call, correlation_id=effective_correlation_id)
            elif model_call.correlation_id != effective_correlation_id:
                raise AgentOrchestrationError("model response correlation ID does not match")
            enforce_execution_mode(model_call, self._execution_mode)
            model_calls.append(model_call)

            parsed_calls = parse_tool_calls(model_call.message)
            if not parsed_calls:
                content = model_call.message.get("content")
                try:
                    if not isinstance(content, str):
                        raise FinalAnswerValidationError(
                            "final model response has no JSON content"
                        )
                    answer = parse_and_validate_json_answer(content, final_schema)
                except FinalAnswerValidationError:
                    # Observed live against Gemma-4-E4B-it/vLLM: structured decoding can
                    # intermittently stall mid-object even at temperature=0, most likely
                    # from floating-point non-associativity under continuous batching
                    # rather than a genuine, reproducible model/schema incompatibility.
                    # Retrying the same request a bounded number of times is a legitimate
                    # transient-failure mitigation, not a silent fallback: it still requires
                    # a fresh, genuine live model call to succeed before returning an answer.
                    if final_answer_retries >= _MAX_FINAL_ANSWER_RETRIES:
                        raise
                    final_answer_retries += 1
                    continue
                return AgentRunResult(
                    role=normalized_role,
                    answer=answer,
                    correlation_id=effective_correlation_id,
                    execution_mode=self._execution_mode,
                    architecture_mode=self._runtime.architecture.mode,
                    model_calls=tuple(model_calls),
                    tool_calls=tuple(tool_executions),
                    latency_ms=_elapsed_ms(started),
                )

            duplicate = next(
                (call.call_id for call in parsed_calls if call.call_id in seen_call_ids),
                None,
            )
            if duplicate is not None:
                raise AgentOrchestrationError(f"model reused tool call ID: {duplicate}")
            if len({call.call_id for call in parsed_calls}) != len(parsed_calls):
                raise AgentOrchestrationError("model emitted duplicate tool call IDs")
            seen_call_ids.update(call.call_id for call in parsed_calls)
            conversation.append(
                {
                    "role": "assistant",
                    "content": (
                        model_call.message.get("content")
                        if model_call.message.get("tool_calls")
                        else None
                    ),
                    "tool_calls": [call.to_openai_dict() for call in parsed_calls],
                }
            )
            for call in parsed_calls:
                execution = await self._registry.execute(
                    call,
                    correlation_id=effective_correlation_id,
                    # The authenticated tenant must always win over whatever tenant the
                    # model wrote into its own tool arguments (tenant isolation).
                    trusted_overrides={"tenant_id": tenant_id},
                )
                tool_executions.append(execution)
                conversation.append(execution.to_tool_message())

        raise AgentOrchestrationError(
            f"agent exceeded the {self._max_model_calls}-call orchestration limit"
        )


def enforce_execution_mode(model_call: ModelCall, mode: ExecutionMode) -> None:
    """Reject failed calls and enforce network-only evidence in live mode."""
    normalized_mode = ExecutionMode(mode)
    status = model_call.status.strip().lower()
    if status not in _OK_STATUSES:
        raise AgentOrchestrationError(f"model call returned non-success status: {status}")
    if normalized_mode is ExecutionMode.OFFLINE_TEST:
        return
    provider = model_call.provider.strip().lower()
    if not model_call.used_network:
        raise LiveInferenceRequiredError("live_required rejected a non-network model result")
    is_offline_provider = any(label in provider for label in _OFFLINE_PROVIDERS)
    if model_call.fallback or is_offline_provider:
        raise LiveInferenceRequiredError("live_required rejected an offline/fallback model result")


def deepcopy_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy tool payloads so provider fakes cannot mutate the registry schemas."""
    return [
        {
            "type": tool["type"],
            "function": {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "parameters": _deepcopy_json(tool["function"]["parameters"]),
            },
        }
        for tool in tools
    ]


def _deepcopy_json(value: Any) -> Any:
    """Copy JSON-compatible schema data without sharing nested provider state."""
    if isinstance(value, dict):
        return {key: _deepcopy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deepcopy_json(item) for item in value]
    return value


def _normalized_role(role: str) -> str:
    """Normalize role keys used for routing and schema names."""
    normalized = role.strip().lower()
    if not _ROLE_NAME.fullmatch(normalized):
        raise ValueError(f"invalid agent role: {role!r}")
    return normalized


def _target_public_dict(target: RoleModelTarget | None) -> dict[str, str] | None:
    """Return a secret-safe role target for readiness and diagnostics."""
    if target is None:
        return None
    return {"endpoint_host": _endpoint_host(target.endpoint), "model": target.model}


def _endpoint_host(endpoint: str) -> str:
    """Strip paths, credentials, and query parameters from endpoint telemetry."""
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if host is None:
        return parsed.netloc.rsplit("@", maxsplit=1)[-1] or parsed.path
    return f"{host}:{parsed.port}" if parsed.port is not None else host


def _elapsed_ms(started: float) -> int:
    """Return non-negative whole milliseconds for one agent run."""
    return max(0, int((perf_counter() - started) * 1000))
