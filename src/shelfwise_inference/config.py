from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


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
            "routing": {
                "routine_agents": ["inventory", "expiry", "demand", "opportunity", "simulation"],
                "strong_agents": ["critic", "executive", "orchestrator"],
            },
        }

    def chat_completions_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"


def _detect_provider(base_url: str) -> ProviderKind:
    lowered = base_url.lower()
    if not lowered:
        return ProviderKind.OFFLINE
    if "fireworks" in lowered:
        return ProviderKind.FIREWORKS
    return ProviderKind.VLLM_MI300X


def load_inference_config() -> InferenceConfig:
    base_url = os.getenv("LLM_BASE_URL", "")
    return InferenceConfig(
        provider=_detect_provider(base_url),
        base_url=base_url,
        routine_model=os.getenv("LLM_ROUTINE_MODEL", os.getenv("LLM_MODEL", "offline-routine")),
        strong_model=os.getenv("LLM_STRONG_MODEL", os.getenv("LLM_MODEL", "offline-strong")),
        api_key=os.getenv("LLM_API_KEY", ""),
        api_key_present=bool(os.getenv("LLM_API_KEY", "")),
    )
