from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .config import InferenceConfig, ProviderKind, load_inference_config

RunRecorder = Callable[[dict[str, Any]], None]
_MAX_RECORDED_CHARS = 4_000


def _is_transient_provider_error(exc: urllib.error.URLError | InferenceError) -> bool:
    """Retry transport failures and explicitly transient HTTP responses once."""
    if isinstance(exc, InferenceError):
        return "error sentinel" in str(exc).casefold()
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    return True


@dataclass(frozen=True, slots=True)
class InferenceResult:
    provider: str
    model: str
    content: str
    used_network: bool
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    run_id: str = ""
    correlation_id: str = ""
    status: str = "ok"
    raw: dict[str, Any] | None = None
    message: dict[str, Any] | None = None
    finish_reason: str = ""
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "content": self.content,
            "used_network": self.used_network,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
            "latency_ms": self.latency_ms,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "status": self.status,
            "raw": self.raw,
            "message": self.message,
            "finish_reason": self.finish_reason,
            "fallback": self.fallback,
        }


class InferenceError(RuntimeError):
    pass


class OpenAICompatibleInferenceClient:
    """Small OpenAI-compatible client for Fireworks and vLLM.

    We avoid SDK lock-in for the hackathon slice. Fireworks and vLLM both expose
    `/v1/chat/completions`, so stdlib HTTP is enough for the first working gateway.
    """

    def __init__(
        self,
        config: InferenceConfig | None = None,
        *,
        recorder: RunRecorder | None = None,
    ) -> None:
        self._config = config or load_inference_config()
        self._recorder = recorder

    @property
    def config(self) -> InferenceConfig:
        return self._config

    def complete(
        self,
        *,
        agent: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        max_tokens: int = 400,
        tenant_id: str = "default",
        correlation_id: str | None = None,
        prompt_version: str = "v1",
        schema_version: str = "v1",
    ) -> InferenceResult:
        """Submit a simple system/user completion through the generic chat transport."""
        return self.chat_completions(
            agent=agent,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )

    def chat_completions(
        self,
        *,
        agent: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 400,
        tenant_id: str = "default",
        correlation_id: str | None = None,
        prompt_version: str = "v1",
        schema_version: str = "v1",
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> InferenceResult:
        """Submit OpenAI-compatible messages and optional tools without SDK coupling.

        `timeout_seconds`, when given, overrides `self.config.timeout_seconds` for this one
        request - callers use it to bound the outbound HTTP call to whatever budget remains
        under a response deadline rather than always requesting the full configured ceiling.
        """
        started = perf_counter()
        run_id = f"mr_{uuid4().hex[:12]}"
        effective_correlation_id = correlation_id or run_id
        effective_model = model or self._config.model_for_agent(agent)
        effective_base_url = (
            self._config.base_url_for_agent(agent) if base_url is None else base_url
        )
        provider = (
            self._config.provider.value
            if effective_base_url == self._config.base_url
            else _provider_label(effective_base_url)
        )
        user = _last_user_text(messages)
        input_tokens = _estimate_payload_tokens(messages, tools, response_format)
        if provider == ProviderKind.OFFLINE.value:
            content = "offline: deterministic ShelfWise cascade is active"
            result = InferenceResult(
                provider=ProviderKind.OFFLINE.value,
                model=effective_model,
                content=content,
                used_network=False,
                input_tokens=input_tokens,
                output_tokens=_estimate_tokens(content),
                latency_ms=_elapsed_ms(started),
                run_id=run_id,
                correlation_id=effective_correlation_id,
                message={"role": "assistant", "content": content},
            )
            self._record_run(
                result,
                agent=agent,
                tenant_id=tenant_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                user=user,
            )
            return result
        effective_api_key = self._config.api_key_for_agent(agent)
        if not effective_api_key:
            self._record_error(
                run_id=run_id,
                correlation_id=effective_correlation_id,
                agent=agent,
                model=effective_model,
                provider=provider,
                tenant_id=tenant_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                latency_ms=_elapsed_ms(started),
                user=user,
                error_detail="LLM_API_KEY is required when LLM_BASE_URL is configured",
            )
            raise InferenceError("LLM_API_KEY is required when LLM_BASE_URL is configured")

        payload: dict[str, Any] = {
            "model": effective_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        elif tool_choice is not None:
            raise InferenceError("tool_choice requires at least one tool")
        if response_format is not None:
            payload["response_format"] = response_format
        url = _chat_completions_url(effective_base_url)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {effective_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        )
        try:
            for attempt in range(2):
                remaining_timeout = effective_timeout - (perf_counter() - started)
                if remaining_timeout <= 0:
                    raise InferenceError("Inference provider request timed out")
                try:
                    with urllib.request.urlopen(
                        request, timeout=remaining_timeout
                    ) as response:
                        raw = json.loads(response.read().decode("utf-8"))
                    message, finish_reason = _extract_message(
                        raw, allow_tool_calls=bool(tools)
                    )
                    content = message.get("content")
                    content = content if isinstance(content, str) else ""
                    if _is_provider_error_sentinel(content):
                        raise InferenceError("Inference provider returned an error sentinel")
                    break
                except (urllib.error.URLError, InferenceError) as exc:
                    if attempt == 0 and _is_transient_provider_error(exc):
                        continue
                    raise
        except (urllib.error.URLError, json.JSONDecodeError, InferenceError) as exc:
            # A transport failure, a non-JSON 200 body, and a well-formed-but-wrong-shape
            # response (missing choices/message/content) are all provider failures - every
            # one of them must be recorded the same way, not just the network case.
            self._record_error(
                run_id=run_id,
                correlation_id=effective_correlation_id,
                agent=agent,
                model=effective_model,
                provider=provider,
                tenant_id=tenant_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                latency_ms=_elapsed_ms(started),
                user=user,
                error_detail=_provider_error_detail(exc),
            )
            raise InferenceError("Inference provider request failed") from exc

        input_tokens, output_tokens = _usage_from_raw(
            raw,
            fallback_input=input_tokens,
            fallback_output=_estimate_tokens(content),
        )
        result = InferenceResult(
            provider=provider,
            model=effective_model,
            content=content,
            used_network=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=_elapsed_ms(started),
            run_id=run_id,
            correlation_id=effective_correlation_id,
            raw=raw,
            message=message,
            finish_reason=finish_reason,
        )
        self._record_run(
            result,
            agent=agent,
            tenant_id=tenant_id,
            prompt_version=prompt_version,
            schema_version=schema_version,
            user=user,
        )
        return result

    def _record_run(
        self,
        result: InferenceResult,
        *,
        agent: str,
        tenant_id: str,
        prompt_version: str,
        schema_version: str,
        user: str,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder(
            {
                "id": result.run_id,
                "tenant_id": tenant_id,
                "correlation_id": result.correlation_id,
                "agent": agent,
                "model": result.model,
                "provider": result.provider,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "status": result.status,
                "user_message": _truncate(user),
                "response_text": _truncate(result.content),
            }
        )

    def _record_error(
        self,
        *,
        run_id: str,
        correlation_id: str,
        agent: str,
        model: str,
        provider: str,
        tenant_id: str,
        prompt_version: str,
        schema_version: str,
        input_tokens: int,
        latency_ms: int,
        user: str,
        error_detail: str,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder(
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "correlation_id": correlation_id,
                "agent": agent,
                "model": model,
                "provider": provider,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "latency_ms": latency_ms,
                "status": "error",
                "user_message": _truncate(user),
                "error_detail": _truncate(error_detail),
            }
        )


def _extract_message(
    raw: dict[str, Any],
    *,
    allow_tool_calls: bool,
) -> tuple[dict[str, Any], str]:
    """Extract one assistant message while allowing content-less tool calls."""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError("Inference response missing choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise InferenceError("Inference response contains an invalid choice")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise InferenceError("Inference response missing message")
    content = message.get("content")
    has_tool_call = bool(message.get("tool_calls")) or isinstance(
        message.get("function_call"), dict
    )
    if not isinstance(content, str) and not (allow_tool_calls and has_tool_call):
        raise InferenceError("Inference response missing text content")
    normalized = dict(message)
    normalized.setdefault("role", "assistant")
    finish_reason = choice.get("finish_reason")
    return normalized, finish_reason if isinstance(finish_reason, str) else ""


def _is_provider_error_sentinel(content: str) -> bool:
    """Reject reverse-proxy error text that was incorrectly wrapped in a HTTP 200."""
    return content.strip().casefold() in {
        "bad gateway",
        "gateway timeout",
        "internal server error",
        "service unavailable",
    }


def _provider_error_detail(exc: Exception) -> str:
    """Include a bounded provider error body so 4xx failures are diagnosable."""
    detail = str(exc)
    if not isinstance(exc, urllib.error.HTTPError):
        return detail
    try:
        body = exc.read(512).decode("utf-8", errors="replace").strip()
    except OSError:
        return detail
    safe_body = " ".join(body.split())
    return f"{detail}: {safe_body}" if safe_body else detail


def _chat_completions_url(base_url: str) -> str:
    """Append the OpenAI chat path while preserving proxy query parameters."""
    scheme, netloc, path, query, fragment = urlsplit(base_url)
    path = path.rstrip("/")
    path = f"{path}/chat/completions" if path.endswith("/v1") else f"{path}/v1/chat/completions"
    return urlunsplit((scheme, netloc, path, query, fragment))


def _provider_label(base_url: str) -> str:
    """Derive a public provider label for endpoint overrides."""
    lowered = base_url.lower()
    if not lowered:
        return ProviderKind.OFFLINE.value
    if "fireworks" in lowered:
        return ProviderKind.FIREWORKS.value
    return ProviderKind.VLLM_MI300X.value


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the last user payload for the existing bounded run recorder."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        return content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    return ""


def _estimate_payload_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
) -> int:
    """Estimate input tokens when a provider omits usage metadata."""
    payload = {"messages": messages}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format
    return _estimate_tokens(json.dumps(payload, sort_keys=True, default=str))


def _usage_from_raw(
    raw: dict[str, Any],
    *,
    fallback_input: int,
    fallback_output: int,
) -> tuple[int, int]:
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return fallback_input, fallback_output
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    return (
        _non_negative_int(prompt_tokens, fallback=fallback_input),
        _non_negative_int(completion_tokens, fallback=fallback_output),
    )


def _non_negative_int(value: object, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, parsed)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _truncate(text: str) -> str:
    """Bound observability payload size; keeps the tail (most recent context) on error."""
    if len(text) <= _MAX_RECORDED_CHARS:
        return text
    return f"...[truncated {len(text) - _MAX_RECORDED_CHARS} chars]...{text[-_MAX_RECORDED_CHARS:]}"
