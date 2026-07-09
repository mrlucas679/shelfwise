from __future__ import annotations

from dataclasses import dataclass

STRONG_AGENTS = {"critic", "executive", "orchestrator"}


@dataclass(frozen=True, slots=True)
class ModelRoute:
    agent: str
    tier: str
    model: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "agent": self.agent,
            "tier": self.tier,
            "model": self.model,
            "reason": self.reason,
        }


def choose_model_route(
    *,
    agent: str,
    routine_model: str,
    strong_model: str,
    risk_tier: str = "low",
) -> ModelRoute:
    if agent in STRONG_AGENTS:
        return ModelRoute(agent, "strong", strong_model, "agent_reviews_or_finalizes_decisions")
    if risk_tier in {"high", "critical"}:
        return ModelRoute(agent, "strong", strong_model, "high_risk_context")
    return ModelRoute(agent, "small", routine_model, "routine_agent")
