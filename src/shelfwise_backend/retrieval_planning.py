"""Deterministic bounded evidence planning and receipts for assistant turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PARTITIONS = (
    "live_facts",
    "decisions",
    "learning",
    "traces",
    "conversation_memory",
    "skills",
)
_SUPPORT_TERMS = {
    "account",
    "login",
    "password",
    "profile",
    "help",
    "invite",
    "invitation",
    "role",
    "sign in",
    "access",
}
_OPERATIONAL_TERMS = {
    "stock",
    "inventory",
    "expiry",
    "expired",
    "demand",
    "forecast",
    "supplier",
    "order",
    "delivery",
    "cold chain",
    "temperature",
    "price",
    "markdown",
    "sku",
    "store",
    "shelf",
    "waste",
    "sales",
    "transfer",
    "product",
}
_DECISION_TERMS = {
    "decision",
    "recommendation",
    "approve",
    "approval",
    "reject",
    "action",
    "attention",
    "why",
    "evidence",
}
_LEARNING_TERMS = {
    "learn",
    "threshold",
    "outcome",
    "performance",
    "accuracy",
    "previous",
    "history",
}
_TRACE_TERMS = {"trace", "audit", "explain", "evidence", "why", "provenance"}
_SCENARIO_TERMS = {"scenario", "simulate", "simulation", "what if", "what-if"}
_HIGH_RISK_TERMS = {"recall", "unsafe", "critical", "approve", "write back", "write-back"}


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """Immutable pre-retrieval selection derived only from request facts."""

    partitions: tuple[str, ...]
    reasons: dict[str, str]
    requires_operational_evidence: bool
    risk_tier: str
    max_followups: int = 1
    policy_version: str = "retrieval-plan-v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "partitions": list(self.partitions),
            "reasons": dict(self.reasons),
            "requires_operational_evidence": self.requires_operational_evidence,
            "risk_tier": self.risk_tier,
            "max_followups": self.max_followups,
            "policy_version": self.policy_version,
        }


def plan_retrieval(
    question: str,
    *,
    has_conversation: bool,
) -> RetrievalPlan:
    """Select the smallest evidence surface needed for the user-visible intent."""
    normalized = " ".join(question.lower().split())
    support_only = _contains(normalized, _SUPPORT_TERMS) and not _contains(
        normalized, _OPERATIONAL_TERMS | _DECISION_TERMS | _SCENARIO_TERMS
    )
    operational = _contains(normalized, _OPERATIONAL_TERMS | _SCENARIO_TERMS)
    selected: set[str] = set()
    reasons: dict[str, str] = {}

    if operational:
        _select(selected, reasons, "live_facts", "operational question requires current facts")
        _select(selected, reasons, "skills", "operational tools are progressively disclosed")
    if _contains(normalized, _DECISION_TERMS | _SCENARIO_TERMS):
        _select(selected, reasons, "decisions", "decision or scenario evidence was requested")
        _select(selected, reasons, "live_facts", "decision evidence must be checked against facts")
        _select(selected, reasons, "skills", "decision tools are progressively disclosed")
    if _contains(normalized, _LEARNING_TERMS | _SCENARIO_TERMS):
        _select(
            selected,
            reasons,
            "learning",
            "historical policy or outcome evidence was requested",
        )
    if _contains(normalized, _TRACE_TERMS):
        _select(selected, reasons, "traces", "audit or provenance evidence was requested")
    if has_conversation:
        _select(
            selected,
            reasons,
            "conversation_memory",
            "the current conversation has prior turns",
        )
    if not selected and not support_only:
        _select(selected, reasons, "live_facts", "general store question requires grounded facts")
        _select(
            selected,
            reasons,
            "skills",
            "general store question may require an operational tool",
        )

    partitions = tuple(name for name in PARTITIONS if name in selected)
    return RetrievalPlan(
        partitions=partitions,
        reasons=reasons,
        requires_operational_evidence=operational or (not support_only and bool(selected)),
        risk_tier="high" if _contains(normalized, _HIGH_RISK_TERMS) else "low",
    )


def build_retrieval_receipt(
    plan: RetrievalPlan,
    evidence: dict[str, object],
) -> dict[str, object]:
    """Describe exactly what was read, omitted, fresh, conflicting, or insufficient."""
    counts = {name: _evidence_count(evidence.get(name)) for name in plan.partitions}
    freshness = {name: _freshness(evidence.get(name)) for name in plan.partitions}
    conflicts = _find_conflicts(evidence)
    operational_count = sum(
        counts.get(name, 0) for name in ("live_facts", "decisions", "learning", "traces")
    )
    insufficient = plan.requires_operational_evidence and operational_count == 0
    followup_allowed = (insufficient or bool(conflicts)) and plan.max_followups > 0
    omissions = {
        name: "not required by deterministic intent and risk signals"
        for name in PARTITIONS
        if name not in plan.partitions
    }
    return {
        "policy_version": plan.policy_version,
        "selected_partitions": list(plan.partitions),
        "counts": counts,
        "omissions": omissions,
        "freshness": freshness,
        "conflicts": conflicts,
        "insufficient_evidence": insufficient,
        "follow_up": {
            "allowed": followup_allowed,
            "used": 0,
            "maximum": plan.max_followups,
            "reason": (
                "one bounded follow-up is permitted for missing or conflicting evidence"
                if followup_allowed
                else "selected evidence is adequate or no operational evidence was required"
            ),
        },
    }


def _select(
    selected: set[str],
    reasons: dict[str, str],
    partition: str,
    reason: str,
) -> None:
    selected.add(partition)
    reasons[partition] = reason


def _contains(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _evidence_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        if not value:
            return 0
        nested = sum(
            len(item)
            for item in value.values()
            if isinstance(item, (list, tuple, set))
        )
        return nested or 1
    return 1


def _freshness(value: object) -> str:
    timestamps: list[str] = []
    _collect_timestamps(value, timestamps)
    parsed = sorted(
        (
            (datetime.fromisoformat(item.replace("Z", "+00:00")), item)
            for item in timestamps
            if _is_timestamp(item)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    return parsed[0][1] if parsed else "unknown"


def _collect_timestamps(value: object, timestamps: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"created_at", "updated_at", "observed_at", "recorded_at"}:
                timestamps.append(str(item))
            elif isinstance(item, (dict, list, tuple)):
                _collect_timestamps(item, timestamps)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_timestamps(item, timestamps)


def _is_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _find_conflicts(evidence: dict[str, object]) -> list[str]:
    conflicts: set[str] = set()
    records: dict[str, str] = {}

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for key in ("conflict", "source_conflict", "has_conflict"):
                if value.get(key):
                    conflicts.add(f"evidence flag: {key}")
            explicit = value.get("conflicts")
            if isinstance(explicit, list):
                conflicts.update(str(item)[:160] for item in explicit if item)
            identity = value.get("id") or value.get("decision_id")
            status = value.get("status")
            if identity and status:
                previous = records.setdefault(str(identity), str(status))
                if previous != str(status):
                    conflicts.add(f"{identity} has conflicting status values")
            for item in value.values():
                inspect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect(item)

    inspect(evidence)
    return sorted(conflicts)
