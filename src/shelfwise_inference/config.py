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

    def model_for_agent(self, agent: str) -> str:
        return self.strong_model if agent in STRONG_AGENT_NAMES else self.routine_model

    def tier_for_agent(self, agent: str) -> ModelTier:
        return ModelTier.STRONG if agent in STRONG_AGENT_NAMES else ModelTier.SMALL

    def to_public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "base_url_configured": bool(self.base_url),
            "routine_model": self.routine_model,
            "strong_model": self.strong_model,
            "api_key_present": self.api_key_present,
            "contract": "openai_chat_completions",
            "base_url_host": _host_label(self.base_url),
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


def _timeout_seconds() -> int:
    """Clamp the network timeout under the 30s hackathon submission response limit."""
    raw = os.getenv("LLM_TIMEOUT_SECONDS", "25")
    try:
        value = int(raw)
    except ValueError:
        value = 25
    return max(1, min(value, 29))


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
    base_url = os.getenv("LLM_BASE_URL", "")
    provider = _detect_provider(base_url)
    return InferenceConfig(
        provider=provider,
        base_url=base_url,
        routine_model=os.getenv("LLM_ROUTINE_MODEL", os.getenv("LLM_MODEL", "offline-routine")),
        strong_model=os.getenv("LLM_STRONG_MODEL", os.getenv("LLM_MODEL", "offline-strong")),
        api_key=os.getenv("LLM_API_KEY", ""),
        api_key_present=bool(os.getenv("LLM_API_KEY", "")),
        timeout_seconds=_timeout_seconds(),
        compute_resource=os.getenv("LLM_COMPUTE_RESOURCE", _default_compute_resource(provider)),
        accelerator=os.getenv("LLM_ACCELERATOR", _default_accelerator(provider)),
    )
