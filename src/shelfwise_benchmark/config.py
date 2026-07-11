from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import (
    AgentSpec,
    BenchmarkConfig,
    EndpointSpec,
    RunSettings,
    StrategyKind,
    StrategySpec,
    WorkflowSpec,
)

_STALE_LOCAL_MARKERS = ("ollama", "direct_local_model", "local_model_probe")


def load_benchmark_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> BenchmarkConfig:
    """Load and validate a benchmark JSON file.

    Args:
        path: JSON configuration path.
        environ: Environment mapping used for endpoint indirection.

    Returns:
        A validated benchmark configuration with no API-key values stored.
    """

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Benchmark config must be a JSON object")
    environment = os.environ if environ is None else environ
    workflow = _parse_workflow(_required_mapping(raw, "workflow"))
    endpoints = _parse_endpoints(_required_mapping(raw, "endpoints"), environment)
    strategies = _parse_strategies(raw.get("strategies"), workflow, endpoints)
    settings = _parse_settings(raw.get("workload", {}))
    return BenchmarkConfig(workflow, endpoints, strategies, settings)


def _parse_workflow(raw: Mapping[str, Any]) -> WorkflowSpec:
    """Parse workflow agents and reject ambiguous execution metadata."""

    name = _required_text(raw, "name")
    agent_rows = raw.get("agents")
    if not isinstance(agent_rows, list) or not agent_rows:
        raise ValueError("workflow.agents must be a non-empty list")
    agents = tuple(_parse_agent(item) for item in agent_rows)
    names = [agent.name for agent in agents]
    if len(names) != len(set(names)):
        raise ValueError("workflow agent names must be unique")
    return WorkflowSpec(name=name, agents=agents)


def _parse_agent(raw: Any) -> AgentSpec:
    """Parse one agent row with bounded generation settings."""

    if not isinstance(raw, dict):
        raise ValueError("Each workflow agent must be an object")
    name = _required_text(raw, "name")
    order = _positive_int(raw.get("order"), f"agent {name} order")
    max_tokens = _positive_int(raw.get("max_tokens", 128), f"agent {name} max_tokens")
    temperature = float(raw.get("temperature", 0.0))
    if not 0.0 <= temperature <= 2.0:
        raise ValueError(f"agent {name} temperature must be between 0 and 2")
    expected = raw.get("expected_terms", [])
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"agent {name} expected_terms must be a string list")
    return AgentSpec(
        name=name,
        order=order,
        parallel_group=str(raw.get("parallel_group") or f"order_{order}"),
        tier=str(raw.get("tier") or "routine"),
        prompt=_required_text(raw, "prompt"),
        system_prompt=str(raw.get("system_prompt") or ""),
        max_tokens=max_tokens,
        temperature=temperature,
        expected_terms=tuple(item for item in expected if item.strip()),
    )


def _parse_endpoints(
    raw: Mapping[str, Any],
    environ: Mapping[str, str],
) -> dict[str, EndpointSpec]:
    """Parse endpoint definitions while resolving only non-secret values."""

    endpoints: dict[str, EndpointSpec] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"Endpoint {name} must be an object")
        endpoint = EndpointSpec(
            name=name,
            base_url=_literal_or_env(item, "base_url", environ),
            model=_literal_or_env(item, "model", environ),
            api_key_env=str(item.get("api_key_env") or ""),
            provider=str(item.get("provider") or "vllm").lower(),
            chat_path=str(item.get("chat_path") or "/v1/chat/completions"),
            metrics_url=_literal_or_env(item, "metrics_url", environ),
            timeout_seconds=float(item.get("timeout_seconds", 60.0)),
        )
        _validate_endpoint(endpoint)
        endpoints[name] = endpoint
    if not endpoints:
        raise ValueError("At least one endpoint definition is required")
    return endpoints


def _parse_strategies(
    raw: Any,
    workflow: WorkflowSpec,
    endpoints: Mapping[str, EndpointSpec],
) -> tuple[StrategySpec, ...]:
    """Parse all four supported topology strategies and route pools."""

    if not isinstance(raw, list) or not raw:
        raise ValueError("strategies must be a non-empty list")
    strategies = tuple(_parse_strategy(item) for item in raw)
    names = [strategy.name for strategy in strategies]
    if len(names) != len(set(names)):
        raise ValueError("strategy names must be unique")
    for strategy in strategies:
        _validate_strategy(strategy, workflow, endpoints)
    return strategies


def _parse_strategy(raw: Any) -> StrategySpec:
    """Parse one strategy and normalize each route to an endpoint tuple."""

    if not isinstance(raw, dict):
        raise ValueError("Each strategy must be an object")
    name = _required_text(raw, "name")
    try:
        kind = StrategyKind(_required_text(raw, "kind"))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in StrategyKind)
        raise ValueError(f"strategy {name} kind must be one of: {allowed}") from exc
    raw_routes = raw.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ValueError(f"strategy {name} routes must be a non-empty object")
    routes = {key: _endpoint_pool(value, name, key) for key, value in raw_routes.items()}
    return StrategySpec(name, kind, routes, str(raw.get("description") or ""))


def _parse_settings(raw: Any) -> RunSettings:
    """Parse the workload ladder controls and reject unsafe values."""

    if not isinstance(raw, dict):
        raise ValueError("workload must be an object")
    maximum = raw.get("max_workflows_per_window")
    settings = RunSettings(
        peak_concurrency=_positive_int(raw.get("peak_concurrency", 64), "peak_concurrency"),
        synchronized_workflows=_positive_int(
            raw.get("synchronized_workflows", 1),
            "synchronized_workflows",
        ),
        warmup_seconds=float(raw.get("warmup_seconds", 5.0)),
        steady_seconds=float(raw.get("steady_seconds", 30.0)),
        repeats=_positive_int(raw.get("repeats", 3), "repeats"),
        telemetry_interval_seconds=float(raw.get("telemetry_interval_seconds", 1.0)),
        max_workflows_per_window=None
        if maximum in (None, 0)
        else _positive_int(maximum, "max_workflows_per_window"),
    )
    if settings.warmup_seconds < 0 or settings.steady_seconds <= 0:
        raise ValueError("warmup_seconds must be non-negative and steady_seconds positive")
    if settings.telemetry_interval_seconds <= 0:
        raise ValueError("telemetry_interval_seconds must be positive")
    return settings


def _validate_endpoint(endpoint: EndpointSpec) -> None:
    """Reject unsupported providers and stale local-model identifiers."""

    if endpoint.provider != "vllm":
        raise ValueError(f"Endpoint {endpoint.name} provider must be vllm")
    identifiers = " ".join((endpoint.name, endpoint.model, endpoint.provider)).lower()
    if any(marker in identifiers for marker in _STALE_LOCAL_MARKERS):
        raise ValueError(f"Endpoint {endpoint.name} contains a stale local-model identifier")
    if endpoint.timeout_seconds <= 0:
        raise ValueError(f"Endpoint {endpoint.name} timeout_seconds must be positive")


def _validate_strategy(
    strategy: StrategySpec,
    workflow: WorkflowSpec,
    endpoints: Mapping[str, EndpointSpec],
) -> None:
    """Ensure route keys match the strategy topology and known endpoints."""

    required = _required_route_keys(strategy.kind, workflow)
    missing = required.difference(strategy.routes)
    if missing:
        values = ", ".join(sorted(missing))
        raise ValueError(f"strategy {strategy.name} is missing routes: {values}")
    routed = {endpoint for pool in strategy.routes.values() for endpoint in pool}
    unknown = routed.difference(endpoints)
    if unknown:
        values = ", ".join(sorted(unknown))
        raise ValueError(f"strategy {strategy.name} references unknown endpoints: {values}")
    if strategy.kind is StrategyKind.REPLICATED and len(strategy.routes["default"]) < 2:
        raise ValueError(f"strategy {strategy.name} replicated route needs at least two endpoints")


def _required_route_keys(kind: StrategyKind, workflow: WorkflowSpec) -> set[str]:
    """Return route keys required by a topology kind."""

    if kind in {StrategyKind.SHARED, StrategyKind.REPLICATED}:
        return {"default"}
    if kind is StrategyKind.PER_AGENT:
        return {agent.name for agent in workflow.agents}
    return {agent.tier for agent in workflow.agents}


def _endpoint_pool(raw: Any, strategy: str, route: str) -> tuple[str, ...]:
    """Normalize a route value to a non-empty endpoint-name tuple."""

    values = [raw] if isinstance(raw, str) else raw
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, str) for item in values)
    ):
        raise ValueError(f"strategy {strategy} route {route} must name one or more endpoints")
    return tuple(values)


def _literal_or_env(
    raw: Mapping[str, Any],
    key: str,
    environ: Mapping[str, str],
) -> str:
    """Resolve a non-secret literal or its `<key>_env` indirection."""

    literal = str(raw.get(key) or "").strip()
    env_name = str(raw.get(f"{key}_env") or "").strip()
    return literal or str(environ.get(env_name, "")).strip()


def _required_mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a required object field with a clear validation error."""

    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    """Return a required non-empty text field."""

    value = str(raw.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    """Return a positive integer or raise a field-specific error."""

    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed
