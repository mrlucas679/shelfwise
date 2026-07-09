from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from .config import InferenceConfig, ProviderKind, load_inference_config

RunRecorder = Callable[[dict[str, Any]], None]
_MAX_RECORDED_CHARS = 4_000


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
        started = perf_counter()
        run_id = f"mr_{uuid4().hex[:12]}"
        effective_correlation_id = correlation_id or run_id
        model = self._config.model_for_agent(agent)
        input_tokens = _estimate_tokens(system) + _estimate_tokens(user)
        if self._config.provider is ProviderKind.OFFLINE:
            content = "offline: deterministic ShelfWise cascade is active"
            result = InferenceResult(
                provider=self._config.provider.value,
                model=model,
                content=content,
                used_network=False,
                input_tokens=input_tokens,
                output_tokens=_estimate_tokens(content),
                latency_ms=_elapsed_ms(started),
                run_id=run_id,
                correlation_id=effective_correlation_id,
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
        if not self._config.api_key_present:
            self._record_error(
                run_id=run_id,
                correlation_id=effective_correlation_id,
                agent=agent,
                model=model,
                tenant_id=tenant_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                latency_ms=_elapsed_ms(started),
                user=user,
                error_detail="LLM_API_KEY is required when LLM_BASE_URL is configured",
            )
            raise InferenceError("LLM_API_KEY is required when LLM_BASE_URL is configured")

        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        url = self._config.chat_completions_url()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
            content = _extract_content(raw)
        except (urllib.error.URLError, json.JSONDecodeError, InferenceError) as exc:
            # A transport failure, a non-JSON 200 body, and a well-formed-but-wrong-shape
            # response (missing choices/message/content) are all provider failures - every
            # one of them must be recorded the same way, not just the network case.
            self._record_error(
                run_id=run_id,
                correlation_id=effective_correlation_id,
                agent=agent,
                model=model,
                tenant_id=tenant_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                latency_ms=_elapsed_ms(started),
                user=user,
                error_detail=str(exc),
            )
            raise InferenceError("Inference provider request failed") from exc

        input_tokens, output_tokens = _usage_from_raw(
            raw,
            fallback_input=input_tokens,
            fallback_output=_estimate_tokens(content),
        )
        result = InferenceResult(
            provider=self._config.provider.value,
            model=model,
            content=content,
            used_network=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=_elapsed_ms(started),
            run_id=run_id,
            correlation_id=effective_correlation_id,
            raw=raw,
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
                "provider": self._config.provider.value,
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


def _extract_content(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError("Inference response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise InferenceError("Inference response missing message")
    content = message.get("content")
    if not isinstance(content, str):
        raise InferenceError("Inference response missing text content")
    return content


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
