"""Deterministic fleet candidate generation for the existing HITL cascade."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from .product_policies import resolve_product_policy

DEFAULT_POLICY_VERSION = "candidate-policy-v1"
DEFAULT_TIME_WINDOW_DAYS = 1
MAX_CANDIDATES = 500


@dataclass(frozen=True, slots=True)
class FleetCandidate:
    """A bounded, model-free candidate that can be promoted to a decision."""

    candidate_key: str
    tenant_id: str
    data_domain: str
    candidate_type: str
    sku: str
    lot_id: str | None
    score: float
    urgency: float
    exposure_units: int
    monitoring_only: bool
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the candidate without losing the source evidence."""
        return {
            "candidate_key": self.candidate_key,
            "tenant_id": self.tenant_id,
            "data_domain": self.data_domain,
            "candidate_type": self.candidate_type,
            "sku": self.sku,
            "lot_id": self.lot_id,
            "score": self.score,
            "urgency": self.urgency,
            "exposure_units": self.exposure_units,
            "monitoring_only": self.monitoring_only,
            "evidence": dict(self.evidence),
        }


def generate_fleet_candidates(
    items: Iterable[dict[str, Any]],
    *,
    tenant_id: str,
    as_of: date | None = None,
    policy_version: str = DEFAULT_POLICY_VERSION,
    time_window_days: int = DEFAULT_TIME_WINDOW_DAYS,
    limit: int = 100,
    open_orders: dict[str, dict[str, Any]] | None = None,
) -> list[FleetCandidate]:
    """Generate stable expiry and stock candidates from product attention rows."""
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    if not policy_version.strip():
        raise ValueError("policy_version is required")
    if time_window_days < 0:
        raise ValueError("time_window_days must be non-negative")
    if limit <= 0 or limit > MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")

    candidates: list[FleetCandidate] = []
    for item in items:
        candidates.extend(
            _item_candidates(
                item,
                tenant_id=tenant_id,
                as_of=as_of,
                policy_version=policy_version,
                time_window_days=time_window_days,
                open_orders=open_orders or {},
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.candidate_key))
    return candidates[:limit]


def _item_candidates(
    item: dict[str, Any],
    *,
    tenant_id: str,
    as_of: date | None,
    policy_version: str,
    time_window_days: int,
    open_orders: dict[str, dict[str, Any]],
) -> list[FleetCandidate]:
    sku = str(item.get("sku") or "").strip()
    if not sku:
        return []
    policy = resolve_product_policy(item.get("category"), item.get("physics"))
    batches = item.get("batches")
    rows = batches if isinstance(batches, list) and batches else [None]
    candidates = []
    for batch in rows:
        if batch is not None and not isinstance(batch, dict):
            continue
        days = _int(batch.get("days_to_expiry")) if batch else _int(item.get("days_to_expiry"))
        units = _int(batch.get("on_hand")) if batch else _int(item.get("on_hand"))
        lot_id = _text(batch.get("lot_id")) if batch else None
        if days < 0:
            candidates.append(
                _candidate(
                    item,
                    tenant_id=tenant_id,
                    candidate_type="expired_lot",
                    lot_id=lot_id,
                    score=100.0 + min(units, 1000) / 100,
                    urgency=1.0,
                    units=units,
                    policy_version=policy_version,
                    time_window_days=time_window_days,
                    as_of=as_of,
                    days=days,
                    policy_id=policy.policy_id,
                )
            )
        elif days <= policy.expiry_review_days:
            candidates.append(
                _candidate(
                    item,
                    tenant_id=tenant_id,
                    candidate_type="expiry_risk",
                    lot_id=lot_id,
                    score=(
                        70.0
                        + (policy.expiry_review_days - days) * 8
                        + min(units, 1000) / 100
                    ),
                    urgency=(policy.expiry_review_days - days + 1)
                    / (policy.expiry_review_days + 1),
                    units=units,
                    policy_version=policy_version,
                    time_window_days=time_window_days,
                    as_of=as_of,
                    days=days,
                    policy_id=policy.policy_id,
                )
            )

    if "low_stock" in set(item.get("attention_reasons") or ()):
        units = _int(item.get("on_hand"))
        reorder = _int(item.get("reorder_point"))
        gap = max(reorder - units, 0)
        order_coverage = open_orders.get(sku) or {}
        candidates.append(
            _candidate(
                item,
                tenant_id=tenant_id,
                candidate_type="low_stock",
                lot_id=None,
                score=35.0 + min(gap, 100) + (15.0 if units == 0 else 0.0),
                urgency=min(1.0, (gap + 1) / max(reorder, 1)),
                units=units,
                policy_version=policy_version,
                time_window_days=time_window_days,
                as_of=as_of,
                days=_int(item.get("days_to_expiry")),
                open_order_units=_int(order_coverage.get("remaining_units")),
                open_order_eta=_text(order_coverage.get("eta")),
                policy_id=policy.policy_id,
            )
        )
    candidates.extend(
        _signal_candidates(
            item,
            tenant_id=tenant_id,
            policy_id=policy.policy_id,
            policy_version=policy_version,
            time_window_days=time_window_days,
            as_of=as_of,
        )
    )
    return candidates


def _signal_candidates(
    item: dict[str, Any],
    *,
    tenant_id: str,
    policy_id: str,
    policy_version: str,
    time_window_days: int,
    as_of: date | None,
) -> list[FleetCandidate]:
    """Turn optional source signals into cheap, typed candidates."""
    units = _int(item.get("on_hand"))
    candidates: list[FleetCandidate] = []
    if bool(item.get("supplier_recent_delay")):
        candidates.append(
            _candidate(
                item,
                tenant_id=tenant_id,
                candidate_type="supplier_delay",
                lot_id=None,
                score=72.0,
                urgency=0.8,
                units=units,
                policy_version=policy_version,
                time_window_days=time_window_days,
                as_of=as_of,
                days=_int(item.get("days_to_expiry")),
                policy_id=policy_id,
                signal="recent supplier delay",
            )
        )

    recent = [_float(value) for value in item.get("recent_daily_units") or []]
    average_daily = sum(recent) / len(recent) if recent else 0.0
    if recent and average_daily <= 1.0 and units > _int(item.get("reorder_point")):
        candidates.append(
            _candidate(
                item,
                tenant_id=tenant_id,
                candidate_type="slow_mover",
                lot_id=None,
                score=55.0 + min(units, 100) / 10,
                urgency=0.35,
                units=units,
                policy_version=policy_version,
                time_window_days=time_window_days,
                as_of=as_of,
                days=_int(item.get("days_to_expiry")),
                policy_id=policy_id,
                signal=f"average daily demand {average_daily:.2f}",
            )
        )
    elif recent and average_daily > 0 and units / average_daily >= 30 and units > _int(
        item.get("reorder_point")
    ):
        candidates.append(
            _candidate(
                item,
                tenant_id=tenant_id,
                candidate_type="overstock",
                lot_id=None,
                score=50.0 + min(units / average_daily, 100) / 5,
                urgency=0.3,
                units=units,
                policy_version=policy_version,
                time_window_days=time_window_days,
                as_of=as_of,
                days=_int(item.get("days_to_expiry")),
                policy_id=policy_id,
                signal=f"{units / average_daily:.1f} days of supply",
            )
        )

    if not item.get("has_batch_evidence", True):
        candidates.append(
            _candidate(
                item,
                tenant_id=tenant_id,
                candidate_type="missing_batch_expiry",
                lot_id=None,
                score=45.0,
                urgency=0.45,
                units=units,
                policy_version=policy_version,
                time_window_days=time_window_days,
                as_of=as_of,
                days=_int(item.get("days_to_expiry")),
                policy_id=policy_id,
                signal="lot-level batch evidence is missing",
            )
        )

    signal_types = (
        ("cold_chain_risk", "cold_chain_risk", 78.0, 0.85),
        ("delivery_missing_units", "delivery_mismatch", 68.0, 0.7),
        ("price_anomaly", "price_promotion_anomaly", 64.0, 0.6),
        ("recall_hold", "recall_compliance_hold", 95.0, 1.0),
        ("identity_conflict", "conflicting_product_identity", 60.0, 0.6),
    )
    for field, candidate_type, score, urgency in signal_types:
        if _truthy_signal(item.get(field)):
            candidates.append(
                _candidate(
                    item,
                    tenant_id=tenant_id,
                    candidate_type=candidate_type,
                    lot_id=None,
                    score=score,
                    urgency=urgency,
                    units=units,
                    policy_version=policy_version,
                    time_window_days=time_window_days,
                    as_of=as_of,
                    days=_int(item.get("days_to_expiry")),
                    policy_id=policy_id,
                    signal=field,
                )
            )
    return candidates


def _candidate(
    item: dict[str, Any],
    *,
    tenant_id: str,
    candidate_type: str,
    lot_id: str | None,
    score: float,
    urgency: float,
    units: int,
    policy_version: str,
    time_window_days: int,
    as_of: date | None,
    days: int,
    open_order_units: int = 0,
    open_order_eta: str | None = None,
    policy_id: str = "unclassified",
    signal: str | None = None,
) -> FleetCandidate:
    sku = str(item["sku"])
    identity = {
        "tenant_id": tenant_id,
        "data_domain": str(item.get("data_domain") or "world_simulation"),
        "candidate_type": candidate_type,
        "sku": sku,
        "lot_id": lot_id,
        "policy_version": policy_version,
        "time_window_days": time_window_days,
    }
    candidate_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    evidence = {
        "name": item.get("name"),
        "category": item.get("category"),
        "supplier": item.get("supplier"),
        "days_to_expiry": days,
        "reorder_point": _int(item.get("reorder_point")),
        "as_of": as_of.isoformat() if as_of else None,
        "policy_version": policy_version,
        "open_order_units": max(open_order_units, 0),
        "open_order_eta": open_order_eta,
        "product_policy": policy_id,
        "signal": signal,
    }
    return FleetCandidate(
        candidate_key=candidate_key,
        tenant_id=tenant_id,
        data_domain=identity["data_domain"],
        candidate_type=candidate_type,
        sku=sku,
        lot_id=lot_id,
        score=round(score, 2),
        urgency=round(max(0.0, min(1.0, urgency)), 3),
        exposure_units=max(units, 0),
        monitoring_only=score < 50.0,
        evidence=evidence,
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _truthy_signal(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "risk", "hold"}


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
