from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    SALES = "sales"
    COLD_CHAIN = "cold_chain"
    EXPIRY = "expiry"
    DEMAND = "demand"
    PROCUREMENT = "procurement"
    OPPORTUNITY = "opportunity"
    SIMULATION = "simulation"
    CRITIC = "critic"
    EXECUTIVE = "executive"


class EventType(StrEnum):
    SCAN = "scan"
    SALE = "sale"
    COLD_CHAIN_ALERT = "cold_chain_alert"
    STOCK_UPDATE = "stock_update"
    EXPIRY_ENTRY = "expiry_entry"
    SUPPLIER_UPDATE = "supplier_update"
    SHIPMENT = "shipment"


class EventSource(StrEnum):
    API = "api"
    MANUAL = "manual"
    POS_CSV = "pos_csv"
    SCANNER = "scanner"
    WMS_CSV = "wms_csv"


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
class Event:
    id: str
    type: EventType
    ts: datetime
    actor: str
    payload: dict[str, Any]
    source: EventSource = EventSource.MANUAL
    tenant_id: str = "default"
    correlation_id: str = ""
    causation_id: str | None = None
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("event id is required")
        if not self.actor:
            raise ValueError("event actor is required")
        if not self.tenant_id:
            raise ValueError("event tenant_id is required")
        if not isinstance(self.payload, dict):
            raise ValueError("event payload must be an object")
        ts = _datetime(self.ts)
        object.__setattr__(self, "type", EventType(self.type))
        object.__setattr__(self, "source", EventSource(self.source))
        object.__setattr__(self, "ts", ts)
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", self.id)

    @classmethod
    def parse_wire(cls, data: dict[str, Any]) -> Event:
        if not isinstance(data, dict):
            raise ValueError("event must be an object")
        allowed = {
            "id",
            "type",
            "ts",
            "actor",
            "payload",
            "source",
            "tenant_id",
            "correlation_id",
            "causation_id",
            "schema_version",
        }
        required = {"id", "type", "ts", "actor", "payload"}
        missing = sorted(key for key in required if key not in data)
        if missing:
            raise ValueError(f"event missing fields: {missing}")
        clean = {key: value for key, value in data.items() if key in allowed}
        clean.setdefault("source", EventSource.API.value)
        clean.setdefault("tenant_id", "default")
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "ts": self.ts.isoformat(),
            "actor": self.actor,
            "payload": deepcopy_json(self.payload),
            "source": self.source.value,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "schema_version": self.schema_version,
        }

    def to_cloudevent(self) -> dict[str, Any]:
        return {
            "specversion": "1.0",
            "id": self.id,
            "type": f"shelfwise.{self.type.value}",
            "source": f"shelfwise://{self.source.value}/{self.actor}",
            "subject": self.actor,
            "time": self.ts.isoformat(),
            "datacontenttype": "application/json",
            "data": deepcopy_json(self.payload),
            "tenantid": self.tenant_id,
            "correlationid": self.correlation_id,
            "causationid": self.causation_id,
            "schema": self.schema_version,
        }


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

    @property
    def must_review(self) -> bool:
        action_risk = RiskTier(self.recommended_action.risk_tier)
        return self.requires_human_review or action_risk is RiskTier.HIGH

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


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("event ts must be an ISO datetime")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def deepcopy_json(value: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        copied[str(key)] = _copy_json_value(item)
    return copied


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return deepcopy_json(value)
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return value


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
