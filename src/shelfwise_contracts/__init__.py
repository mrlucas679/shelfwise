from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class AgentName(StrEnum):
    INVENTORY = "inventory"
    EXPIRY = "expiry"
    DEMAND = "demand"
    OPPORTUNITY = "opportunity"
    SIMULATION = "simulation"
    CRITIC = "critic"
    EXECUTIVE = "executive"


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Money:
    minor_units: int
    currency: str = "ZAR"

    @classmethod
    def zar(cls, rand: object) -> Money:
        cents = (_decimal(rand) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return cls(minor_units=int(cents), currency="ZAR")

    @property
    def amount(self) -> Decimal:
        return (Decimal(self.minor_units) / 100).quantize(Decimal("0.01"))

    @property
    def cents(self) -> int:
        return self.minor_units

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __mul__(self, factor: object) -> Money:
        cents = (Decimal(self.minor_units) * _decimal(factor)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return Money(int(cents), self.currency)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minor_units": self.minor_units,
            "currency": self.currency,
            "amount": str(self.amount),
        }

    def __str__(self) -> str:
        return f"{self.currency} {self.amount}"


@dataclass(frozen=True, slots=True)
class SourceRef:
    kind: str
    ref: str
    locator: str | None = None

    @classmethod
    def dataset(cls, ref: str, locator: str | None = None) -> SourceRef:
        return cls(kind="dataset", ref=ref, locator=locator)

    @classmethod
    def tool(cls, ref: str, locator: str | None = None) -> SourceRef:
        return cls(kind="tool", ref=ref, locator=locator)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "locator": self.locator}

    def __str__(self) -> str:
        return f"{self.ref}#{self.locator}" if self.locator else self.ref


@dataclass(frozen=True, slots=True)
class RecommendedAction:
    type: str
    params: dict[str, Any]
    risk_tier: RiskTier

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "params": self.params, "risk_tier": self.risk_tier.value}


@dataclass(frozen=True, slots=True)
class EvidenceObject:
    agent: AgentName
    conclusion: str
    supporting_data: list[dict[str, Any]]
    confidence: Decimal
    recommended_action: RecommendedAction
    sources: tuple[SourceRef, ...]
    requires_human_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent.value,
            "conclusion": self.conclusion,
            "supporting_data": self.supporting_data,
            "confidence": str(self.confidence),
            "recommended_action": self.recommended_action.to_dict(),
            "sources": [source.to_dict() for source in self.sources],
            "requires_human_review": self.requires_human_review,
        }


@dataclass(frozen=True, slots=True)
class TraceSpan:
    name: str
    status: str
    ms: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "ms": self.ms, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    status: DecisionStatus
    action: RecommendedAction
    caused_by: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "action": self.action.to_dict(),
            "caused_by": list(self.caused_by),
            "summary": self.summary,
        }
