from __future__ import annotations

import ipaddress
import itertools
import os
from collections.abc import Mapping
from urllib.parse import urlparse

from .models import AgentSpec, EndpointSpec, EvidenceScope, StrategyKind, StrategySpec


class StrategyRouter:
    """Resolve agents to endpoint replicas for a single strategy."""

    def __init__(
        self,
        strategy: StrategySpec,
        endpoints: Mapping[str, EndpointSpec],
    ) -> None:
        """Store route metadata and initialize deterministic round-robin counters."""

        self.strategy = strategy
        self.endpoints = endpoints
        self._counters: dict[str, itertools.count[int]] = {}

    def resolve(self, agent: AgentSpec) -> EndpointSpec:
        """Return the next endpoint for an agent's route pool."""

        route_key = self._route_key(agent)
        pool = self.strategy.routes[route_key]
        counter = self._counters.setdefault(route_key, itertools.count())
        endpoint_name = pool[next(counter) % len(pool)]
        return self.endpoints[endpoint_name]

    def endpoint_names(self) -> tuple[str, ...]:
        """Return every endpoint used by the strategy without duplicates."""

        names = {endpoint for pool in self.strategy.routes.values() for endpoint in pool}
        return tuple(sorted(names))

    def _route_key(self, agent: AgentSpec) -> str:
        """Select the route dimension required by the strategy kind."""

        if self.strategy.kind in {StrategyKind.SHARED, StrategyKind.REPLICATED}:
            return "default"
        if self.strategy.kind is StrategyKind.PER_AGENT:
            return agent.name
        return agent.tier


def strategy_unavailable_reason(
    router: StrategyRouter,
    scope: EvidenceScope,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return an honest preflight reason, or an empty string when runnable.

    Args:
        router: Strategy router whose endpoints should be checked.
        scope: Declared benchmark evidence scope.
        environ: Environment mapping used to check named API keys.

    Returns:
        Empty text when every routed endpoint is usable, otherwise a safe reason.
    """

    environment = os.environ if environ is None else environ
    for name in router.endpoint_names():
        endpoint = router.endpoints[name]
        if not endpoint.configured:
            return f"endpoint {name} is missing base_url or model configuration"
        if endpoint.api_key_env and not environment.get(endpoint.api_key_env):
            return f"endpoint {name} is missing environment variable {endpoint.api_key_env}"
        if scope is EvidenceScope.CONTROL_PLANE_ONLY and is_loopback_url(endpoint.base_url):
            return f"endpoint {name} is loopback and cannot be local inference evidence"
    return ""


def is_loopback_url(value: str) -> bool:
    """Return whether a URL targets localhost or a loopback address."""

    host = (urlparse(value).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
