from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from shelfwise_contracts import Money

from .utils import clamp, decimal, q2

RISK_PENALTY = {
    "low": Decimal("0.00"),
    "medium": Decimal("0.10"),
    "high": Decimal("0.35"),
    "critical": Decimal("0.75"),
}


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    action_type: str
    expected_value: Money
    confidence: Decimal
    risk_band: str
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class RankedAction:
    candidate: ActionCandidate
    score: Decimal
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action_type": self.candidate.action_type,
            "expected_value": self.candidate.expected_value.to_dict(),
            "confidence": str(self.candidate.confidence),
            "risk_band": self.candidate.risk_band,
            "score": str(self.score),
            "method": self.method,
            "explanation": self.candidate.explanation,
        }


def rank_actions(candidates: list[ActionCandidate]) -> list[RankedAction]:
    ranked: list[RankedAction] = []
    for candidate in candidates:
        risk_key = candidate.risk_band.lower()
        if risk_key not in RISK_PENALTY:
            raise ValueError(f"unsupported risk_band: {candidate.risk_band}")
        penalty = RISK_PENALTY[risk_key]
        score = (
            decimal(candidate.expected_value.minor_units)
            * clamp(candidate.confidence)
            * (Decimal("1") - penalty)
        )
        ranked.append(
            RankedAction(
                candidate=candidate,
                score=q2(score),
                method="expected_value_with_risk_penalty",
            )
        )
    return sorted(ranked, key=lambda item: item.score, reverse=True)
