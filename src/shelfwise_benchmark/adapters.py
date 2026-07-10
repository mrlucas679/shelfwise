from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import AgentSpec, EndpointSpec, RequestOutcome

_PROMETHEUS_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{[^}]*\})?\s+(?P<value>[-+a-zA-Z0-9.eE]+)(?:\s+\d+)?$"
)


class InferenceAdapter(Protocol):
    """Define the asynchronous provider contract used by the load runner."""

    async def complete(
        self,
        endpoint: EndpointSpec,
        agent: AgentSpec,
        request_id: str,
    ) -> RequestOutcome:
        """Execute one agent completion and return sanitized metrics."""

    async def aclose(self) -> None:
        """Release adapter resources."""


@dataclass(slots=True)
class VllmMetricsSnapshot:
    """Represent one parsed vLLM Prometheus endpoint observation."""

    endpoint: str
    available: bool
    reason: str = ""
    queue_length: float | None = None
    running_requests: float | None = None
    queue_time_sum_seconds: float | None = None
    queue_time_count: float | None = None
    inference_time_sum_seconds: float | None = None
    inference_time_count: float | None = None


class VllmAdapter:
    """Call OpenAI-compatible vLLM chat completions and parse request metrics."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Accept an injectable client and environment for deterministic tests."""

        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._environ = os.environ if environ is None else environ

    async def complete(
        self,
        endpoint: EndpointSpec,
        agent: AgentSpec,
        request_id: str,
    ) -> RequestOutcome:
        """Execute one vLLM request and return tokens, timing, and quality."""

        headers = {"X-Request-ID": request_id}
        if endpoint.api_key_env:
            headers["Authorization"] = f"Bearer {self._environ[endpoint.api_key_env]}"
        payload = _chat_payload(endpoint, agent)
        started = time.perf_counter()
        try:
            response = await self._client.post(
                endpoint_chat_url(endpoint),
                json=payload,
                headers=headers,
                timeout=endpoint.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return _failed_outcome(type(exc).__name__.lower(), _elapsed_ms(started))
        latency_ms = _elapsed_ms(started)
        if not response.is_success:
            return _failed_outcome(f"http_{response.status_code}", latency_ms, response.status_code)
        try:
            body = response.json()
        except ValueError:
            return _failed_outcome("invalid_json", latency_ms, response.status_code)
        return parse_vllm_response(body, latency_ms, response.status_code, agent.expected_terms)

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._owns_client:
            await self._client.aclose()


class ControlPlaneAdapter:
    """Exercise orchestration without making or claiming model calls."""

    async def complete(
        self,
        endpoint: EndpointSpec,
        agent: AgentSpec,
        request_id: str,
    ) -> RequestOutcome:
        """Return a no-inference outcome for a plan-only control-plane run."""

        del endpoint, agent, request_id
        await asyncio.sleep(0)
        return RequestOutcome(
            success=True,
            model_call=False,
            status_code=None,
            latency_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            queue_time_ms=None,
            inference_time_ms=None,
            time_to_first_token_ms=None,
            generation_time_ms=None,
            mean_inter_token_latency_ms=None,
            tokens_per_second=None,
            quality_score=None,
        )

    async def aclose(self) -> None:
        """Complete the adapter lifecycle without external resources."""


class VllmMetricsClient:
    """Fetch and parse server-level metrics from vLLM `/metrics`."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Accept an injectable client and environment for endpoint auth."""

        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._environ = os.environ if environ is None else environ

    async def sample(self, endpoint: EndpointSpec) -> VllmMetricsSnapshot:
        """Read one Prometheus snapshot and retain only benchmark metrics."""

        headers: dict[str, str] = {}
        if endpoint.api_key_env:
            headers["Authorization"] = f"Bearer {self._environ[endpoint.api_key_env]}"
        try:
            response = await self._client.get(
                endpoint_metrics_url(endpoint),
                headers=headers,
                timeout=min(endpoint.timeout_seconds, 10.0),
            )
        except httpx.HTTPError as exc:
            return VllmMetricsSnapshot(endpoint.name, False, type(exc).__name__.lower())
        if not response.is_success:
            return VllmMetricsSnapshot(endpoint.name, False, f"http_{response.status_code}")
        metrics = parse_prometheus_metrics(response.text)
        if not metrics:
            return VllmMetricsSnapshot(endpoint.name, False, "metrics_empty_or_unparseable")
        return _snapshot_from_metrics(endpoint.name, metrics)

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._owns_client:
            await self._client.aclose()


def parse_vllm_response(
    body: Any,
    latency_ms: float,
    status_code: int,
    expected_terms: tuple[str, ...] = (),
) -> RequestOutcome:
    """Parse OpenAI usage and vLLM's optional per-request metrics object."""

    if not isinstance(body, dict):
        return _failed_outcome("invalid_response_shape", latency_ms, status_code)
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else {}
    prompt_tokens = _optional_int(usage.get("prompt_tokens"))
    completion_tokens = _optional_int(usage.get("completion_tokens"))
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    ttft = _optional_float(metrics.get("time_to_first_token_ms"))
    generation = _optional_float(metrics.get("generation_time_ms"))
    inference = None if ttft is None or generation is None else ttft + generation
    content = _response_content(body)
    return RequestOutcome(
        success=True,
        model_call=True,
        status_code=status_code,
        latency_ms=round(latency_ms, 3),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        queue_time_ms=_optional_float(metrics.get("queue_time_ms")),
        inference_time_ms=inference,
        time_to_first_token_ms=ttft,
        generation_time_ms=generation,
        mean_inter_token_latency_ms=_optional_float(metrics.get("mean_itl_ms")),
        tokens_per_second=_optional_float(metrics.get("tokens_per_second")),
        quality_score=score_expected_terms(content, expected_terms),
    )


def parse_prometheus_metrics(text: str) -> dict[str, list[float]]:
    """Parse numeric Prometheus samples without an additional dependency."""

    parsed: dict[str, list[float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROMETHEUS_LINE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isfinite(value):
            parsed.setdefault(match.group("name"), []).append(value)
    return parsed


def endpoint_chat_url(endpoint: EndpointSpec) -> str:
    """Join a base URL and chat path without duplicating `/v1`."""

    if endpoint.chat_path.startswith(("http://", "https://")):
        return endpoint.chat_path
    base = endpoint.base_url.rstrip("/")
    path = "/" + endpoint.chat_path.lstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def endpoint_metrics_url(endpoint: EndpointSpec) -> str:
    """Return an explicit metrics URL or derive the server-root endpoint."""

    if endpoint.metrics_url:
        return endpoint.metrics_url
    parsed = urlsplit(endpoint.base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/metrics", "", ""))


def score_expected_terms(content: str, expected_terms: tuple[str, ...]) -> float | None:
    """Score deterministic expected-term coverage when a quality rubric exists."""

    if not expected_terms:
        return None
    normalized = content.casefold()
    matched = sum(term.casefold() in normalized for term in expected_terms)
    return round(matched / len(expected_terms), 6)


def _chat_payload(endpoint: EndpointSpec, agent: AgentSpec) -> dict[str, Any]:
    """Build a deterministic single-sequence OpenAI chat request."""

    messages = []
    if agent.system_prompt:
        messages.append({"role": "system", "content": agent.system_prompt})
    messages.append({"role": "user", "content": agent.prompt})
    return {
        "model": endpoint.model,
        "messages": messages,
        "max_tokens": agent.max_tokens,
        "temperature": agent.temperature,
        "n": 1,
        "stream": False,
    }


def _snapshot_from_metrics(
    endpoint: str,
    metrics: Mapping[str, list[float]],
) -> VllmMetricsSnapshot:
    """Extract queue and inference aggregates across vLLM metric aliases."""

    queue = _metric_total(metrics, "vllm:num_requests_waiting", "vllm_num_requests_waiting")
    running = _metric_total(metrics, "vllm:num_requests_running", "vllm_num_requests_running")
    return VllmMetricsSnapshot(
        endpoint=endpoint,
        available=queue is not None or running is not None,
        reason="" if queue is not None or running is not None else "required_metrics_unavailable",
        queue_length=queue,
        running_requests=running,
        queue_time_sum_seconds=_metric_total(
            metrics,
            "vllm:request_queue_time_seconds_sum",
            "vllm_request_queue_time_seconds_sum",
        ),
        queue_time_count=_metric_total(
            metrics,
            "vllm:request_queue_time_seconds_count",
            "vllm_request_queue_time_seconds_count",
        ),
        inference_time_sum_seconds=_metric_total(
            metrics,
            "vllm:request_inference_time_seconds_sum",
            "vllm_request_inference_time_seconds_sum",
        ),
        inference_time_count=_metric_total(
            metrics,
            "vllm:request_inference_time_seconds_count",
            "vllm_request_inference_time_seconds_count",
        ),
    )


def _metric_total(metrics: Mapping[str, list[float]], *names: str) -> float | None:
    """Sum all label variants for the first available metric alias."""

    for name in names:
        if name in metrics:
            return sum(metrics[name])
    return None


def _response_content(body: Mapping[str, Any]) -> str:
    """Extract text from the first OpenAI chat completion choice."""

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _failed_outcome(
    error_code: str,
    latency_ms: float,
    status_code: int | None = None,
) -> RequestOutcome:
    """Create a sanitized failed provider outcome without raw exceptions."""

    return RequestOutcome(
        success=False,
        model_call=True,
        status_code=status_code,
        latency_ms=round(latency_ms, 3),
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        queue_time_ms=None,
        inference_time_ms=None,
        time_to_first_token_ms=None,
        generation_time_ms=None,
        mean_inter_token_latency_ms=None,
        tokens_per_second=None,
        quality_score=None,
        error_code=error_code,
    )


def _optional_int(value: Any) -> int | None:
    """Return an integer metric only when the provider supplied one."""

    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    """Return a finite floating-point metric or mark it unavailable."""

    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _elapsed_ms(started: float) -> float:
    """Return elapsed high-resolution time in milliseconds."""

    return (time.perf_counter() - started) * 1000
