from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse, urlsplit, urlunsplit


class ProviderKind(StrEnum):
    FIREWORKS = "fireworks"
    VLLM_MI300X = "vllm_mi300x"
    OFFLINE = "offline"


class ModelTier(StrEnum):
    SMALL = "small"
    STRONG = "strong"


STRONG_AGENT_NAMES = {"critic", "executive", "orchestrator"}


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    """Provider/model routing for ShelfWise.

    One OpenAI-compatible client will use this config later. For now it documents the real runtime
    contract: Fireworks for managed reliability, AMD Developer Cloud/vLLM for direct MI300X proof,
    and stronger models for Critic + Executive/Orchestrator.
    """

    provider: ProviderKind
    base_url: str
    routine_model: str
    strong_model: str
    api_key: str
    api_key_present: bool
    timeout_seconds: int = 25
    compute_resource: str = ""
    accelerator: str = ""
    routine_base_url: str = ""
    strong_base_url: str = ""
    routine_api_key: str = ""
    strong_api_key: str = ""

    def model_for_agent(self, agent: str) -> str:
        return self.strong_model if agent in STRONG_AGENT_NAMES else self.routine_model

    def tier_for_agent(self, agent: str) -> ModelTier:
        return ModelTier.STRONG if agent in STRONG_AGENT_NAMES else ModelTier.SMALL

    def base_url_for_agent(self, agent: str) -> str:
        if self.tier_for_agent(agent) is ModelTier.STRONG:
            return self.strong_base_url or self.base_url
        return self.routine_base_url or self.base_url

    def api_key_for_agent(self, agent: str) -> str:
        if self.tier_for_agent(agent) is ModelTier.STRONG:
            return self.strong_api_key or self.api_key
        return self.routine_api_key or self.api_key

    @property
    def dual_model_configured(self) -> bool:
        return bool(
            self.routine_model != self.strong_model
            and self.base_url_for_agent("inventory")
            and self.base_url_for_agent("executive")
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "base_url_configured": bool(self.base_url),
            "routine_model": self.routine_model,
            "strong_model": self.strong_model,
            "api_key_present": self.api_key_present,
            "contract": "openai_chat_completions",
            "base_url_host": _host_label(self.base_url),
            "routine_base_url_host": _host_label(self.base_url_for_agent("inventory")),
            "strong_base_url_host": _host_label(self.base_url_for_agent("executive")),
            "dual_model_configured": self.dual_model_configured,
            "timeout_seconds": self.timeout_seconds,
            "compute_resource": self.compute_resource,
            "accelerator": self.accelerator,
            "routing": {
                "routine_agents": ["inventory", "expiry", "demand", "opportunity", "simulation"],
                "strong_agents": ["critic", "executive", "orchestrator"],
            },
        }

    def chat_completions_url(self) -> str:
        # Use urlsplit/urlunsplit (not plain string concatenation) so a base_url that
        # already carries a query string (e.g. a proxied notebook endpoint requiring
        # `?token=...`) still gets the path appended *before* the query, not after it.
        scheme, netloc, path, query, fragment = urlsplit(self.base_url)
        path = path.rstrip("/")
        path = f"{path}/chat/completions" if path.endswith("/v1") else f"{path}/v1/chat/completions"
        return urlunsplit((scheme, netloc, path, query, fragment))


def _detect_provider(base_url: str) -> ProviderKind:
    lowered = base_url.lower()
    if not lowered:
        return ProviderKind.OFFLINE
    if "fireworks" in lowered:
        return ProviderKind.FIREWORKS
    return ProviderKind.VLLM_MI300X


def _host_label(base_url: str) -> str:
    """Expose only the host portion of the endpoint - enough for proof, no secrets."""
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    return parsed.netloc or parsed.path


MAX_OPERATIONAL_TIMEOUT_S = 900
"""Upper bound that prevents an accidental unbounded network wait during real-app testing."""


def _timeout_seconds() -> int:
    """Return the configured inference timeout within the operational safety bound."""
    raw = os.getenv("LLM_TIMEOUT_SECONDS", "120")
    try:
        value = int(raw)
    except ValueError:
        value = 120
    return max(1, min(value, MAX_OPERATIONAL_TIMEOUT_S))


def _default_compute_resource(provider: ProviderKind) -> str:
    if provider is ProviderKind.VLLM_MI300X:
        return "AMD Developer Cloud"
    if provider is ProviderKind.FIREWORKS:
        return "Fireworks AI"
    return "local deterministic fallback"


def _default_accelerator(provider: ProviderKind) -> str:
    if provider is ProviderKind.VLLM_MI300X:
        return "AMD Instinct MI300X"
    return ""


def load_inference_config() -> InferenceConfig:
    common_base_url = os.getenv("LLM_BASE_URL", "")
    routine_base_url = os.getenv("LLM_ROUTINE_BASE_URL", common_base_url)
    strong_base_url = os.getenv("LLM_STRONG_BASE_URL", common_base_url)
    base_url = common_base_url or routine_base_url or strong_base_url
    api_key = os.getenv("LLM_API_KEY", "")
    routine_api_key = os.getenv("LLM_ROUTINE_API_KEY", api_key)
    strong_api_key = os.getenv("LLM_STRONG_API_KEY", api_key)
    provider = _detect_provider(base_url)
    return InferenceConfig(
        provider=provider,
        base_url=base_url,
        routine_model=os.getenv("LLM_ROUTINE_MODEL", os.getenv("LLM_MODEL", "offline-routine")),
        strong_model=os.getenv("LLM_STRONG_MODEL", os.getenv("LLM_MODEL", "offline-strong")),
        api_key=api_key,
        api_key_present=bool(routine_api_key or strong_api_key),
        timeout_seconds=_timeout_seconds(),
        compute_resource=os.getenv("LLM_COMPUTE_RESOURCE", _default_compute_resource(provider)),
        accelerator=os.getenv("LLM_ACCELERATOR", _default_accelerator(provider)),
        routine_base_url=routine_base_url,
        strong_base_url=strong_base_url,
        routine_api_key=routine_api_key,
        strong_api_key=strong_api_key,
    )
