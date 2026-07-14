"""Deterministic HITL SLA and workload-cap calculations."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

DEFAULT_SLA_MINUTES = {"high": 60, "medium": 240, "low": 1_440}
DEFAULT_ROLE_CAPS = {
    "inventory_manager": 20,
    "procurement_manager": 20,
    "sales_manager": 20,
    "facilities_manager": 12,
    "manager": 30,
    "owner": 50,
}


@dataclass(frozen=True, slots=True)
class SlaAssessment:
    """The current review pressure for one pending decision."""

    decision_id: str
    role: str
    risk_tier: str
    age_seconds: int
    sla_seconds: int
    status: str
    escalation_required: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize the assessment for observability and review queues."""
        return {
            "decision_id": self.decision_id,
            "role": self.role,
            "risk_tier": self.risk_tier,
            "age_seconds": self.age_seconds,
            "sla_seconds": self.sla_seconds,
            "status": self.status,
            "escalation_required": self.escalation_required,
        }


def assess_decision_sla(
    decision: dict[str, Any], *, now: datetime | None = None
) -> SlaAssessment | None:
    """Classify a pending decision against its risk-tier review SLA."""
    if decision.get("status") != "pending":
        return None
    created = _parse_timestamp(decision.get("created_at"))
    if created is None:
        return None
    current = _utc(now or datetime.now(UTC))
    age_seconds = max(0, int((current - created).total_seconds()))
    risk_tier = _risk_tier(decision)
    sla_seconds = _sla_minutes(risk_tier) * 60
    ratio = age_seconds / max(sla_seconds, 1)
    status = "breached" if ratio >= 1 else "at_risk" if ratio >= 0.75 else "on_track"
    return SlaAssessment(
        decision_id=str(decision.get("id") or "unknown"),
        role=str(decision.get("role") or "unknown"),
        risk_tier=risk_tier,
        age_seconds=age_seconds,
        sla_seconds=sla_seconds,
        status=status,
        escalation_required=status == "breached" or (risk_tier == "high" and ratio >= 0.75),
    )


def build_workload_snapshot(
    decisions: list[dict[str, Any]],
    *,
    candidates: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return bounded SLA, role-cap, and suppression pressure metrics."""
    assessments = [
        assessment
        for decision in decisions
        if (assessment := assess_decision_sla(decision, now=now)) is not None
    ]
    role_counts = Counter(item.role for item in assessments)
    caps = role_caps()
    cap_breaches = {
        role: count
        for role, count in role_counts.items()
        if count > caps.get(role, caps["manager"])
    }
    statuses = Counter(item.status for item in assessments)
    ages = [item.age_seconds for item in assessments]
    candidate_rows = candidates or []
    return {
        "pending": len(assessments),
        "sla_status_counts": dict(sorted(statuses.items())),
        "oldest_pending_age_seconds": max(ages, default=0),
        "at_risk": statuses["at_risk"],
        "breached": statuses["breached"],
        "escalation_required": sum(1 for item in assessments if item.escalation_required),
        "role_counts": dict(sorted(role_counts.items())),
        "role_caps": caps,
        "role_cap_breaches": cap_breaches,
        "suppressed_candidates": sum(
            1 for item in candidate_rows if item.get("status") == "suppressed"
        ),
        "assessments": [item.to_dict() for item in assessments[:100]],
    }


def role_caps() -> dict[str, int]:
    """Return configurable role caps without accepting unsafe zero/negative values."""
    caps = dict(DEFAULT_ROLE_CAPS)
    for role in tuple(caps):
        raw = os.getenv(f"SHELFWISE_HITL_CAP_{role.upper()}")
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            caps[role] = value
    return caps


def _risk_tier(decision: dict[str, Any]) -> str:
    action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
    risk = str(action.get("risk_tier") or decision.get("risk_tier") or "medium").lower()
    return risk if risk in DEFAULT_SLA_MINUTES else "medium"


def _sla_minutes(risk_tier: str) -> int:
    raw = os.getenv(f"SHELFWISE_SLA_{risk_tier.upper()}_MINUTES")
    if raw is None:
        return DEFAULT_SLA_MINUTES[risk_tier]
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SLA_MINUTES[risk_tier]
    return value if value > 0 else DEFAULT_SLA_MINUTES[risk_tier]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
