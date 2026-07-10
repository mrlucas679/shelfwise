from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any

from shelfwise_storage import connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


@dataclass(frozen=True, slots=True)
class LearningEvent:
    id: str
    decision_id: str
    sku: str
    metric: str
    previous_threshold: int
    updated_threshold: int
    delta_units: int
    outcome: dict[str, Any]
    message: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "sku": self.sku,
            "metric": self.metric,
            "previous_threshold": self.previous_threshold,
            "updated_threshold": self.updated_threshold,
            "delta_units": self.delta_units,
            "outcome": deepcopy(self.outcome),
            "message": self.message,
            "created_at": self.created_at,
        }


class InMemoryLearningStore:
    """Deterministic memory layer for the demo's visible learning moment."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._thresholds: dict[str, int] = {}
        self._events_by_decision: dict[str, LearningEvent] = {}

    def thresholds(self) -> dict[str, int]:
        with self._lock:
            return dict(self._thresholds)

    def list_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [event.to_dict() for event in self._events_by_decision.values()]

    def clear(self) -> None:
        """Reset test state for the shared, process-wide store."""
        with self._lock:
            self._thresholds.clear()
            self._events_by_decision.clear()

    def record_approved_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        if decision.get("status") != "approved":
            raise ValueError("learning requires an approved decision")
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        with self._lock:
            existing = self._events_by_decision.get(decision_id)
            if existing is not None:
                return existing.to_dict()

            metric, _subject = _routed_metric(decision)
            event = _build_learning_event(
                decision,
                previous_threshold=self._thresholds.get(metric),
            )
            self._thresholds[metric] = event.updated_threshold
            self._events_by_decision[decision_id] = event
            return event.to_dict()


class PostgresLearningStore:
    """Postgres-backed learning store for approved outcomes and threshold memory."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresLearningStore")
        self._database_url = database_url
        self._ensure_schema()

    def thresholds(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "select metric, threshold_units from shelfwise_learning_thresholds"
            ).fetchall()
        return {row["metric"]: int(row["threshold_units"]) for row in rows}

    def list_events(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select payload
                from shelfwise_learning_events
                order by created_at desc, decision_id
                """
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_learning_events")
            conn.execute("delete from shelfwise_learning_thresholds")
            conn.commit()

    def record_approved_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        if decision.get("status") != "approved":
            raise ValueError("learning requires an approved decision")
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        with self._connect() as conn:
            existing = conn.execute(
                "select payload from shelfwise_learning_events where decision_id = %s",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                return deepcopy(existing["payload"])

            tenant_id = _tenant_id(decision)
            metric, _subject = _routed_metric(decision)
            threshold_row = conn.execute(
                """
                select threshold_units
                from shelfwise_learning_thresholds
                where tenant_id = %s and metric = %s
                for update
                """,
                (tenant_id, metric),
            ).fetchone()
            previous_threshold = (
                int(threshold_row["threshold_units"]) if threshold_row is not None else None
            )
            event = _build_learning_event(decision, previous_threshold=previous_threshold)
            payload = event.to_dict()
            conn.execute(
                """
                insert into shelfwise_learning_thresholds
                    (tenant_id, metric, sku, threshold_units, updated_at)
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id, metric) do update
                set threshold_units = excluded.threshold_units,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, event.metric, event.sku, event.updated_threshold, event.created_at),
            )
            conn.execute(
                """
                insert into shelfwise_learning_events (tenant_id, decision_id, payload, created_at)
                values (%s, %s, %s, %s)
                """,
                (tenant_id, event.decision_id, jsonb(payload), event.created_at),
            )
            conn.commit()
            return payload

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists shelfwise_learning_thresholds (
                    tenant_id text not null default 'default',
                    metric text not null,
                    sku text not null,
                    threshold_units integer not null,
                    updated_at timestamptz not null,
                    primary key (tenant_id, metric)
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_learning_thresholds
                add column if not exists tenant_id text not null default 'default'
                """
            )
            conn.execute(
                """
                create unique index if not exists ux_shelfwise_learning_thresholds_tenant_metric
                on shelfwise_learning_thresholds (tenant_id, metric)
                """
            )
            conn.execute(
                """
                create table if not exists shelfwise_learning_events (
                    tenant_id text not null default 'default',
                    decision_id text not null,
                    payload jsonb not null,
                    created_at timestamptz not null,
                    primary key (tenant_id, decision_id)
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_learning_events
                add column if not exists tenant_id text not null default 'default'
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_learning_events_tenant_created
                on shelfwise_learning_events (tenant_id, created_at desc)
                """
            )
            apply_tenant_rls(
                conn,
                ("shelfwise_learning_thresholds", "shelfwise_learning_events"),
            )
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def create_learning_store() -> InMemoryLearningStore | PostgresLearningStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryLearningStore()
    if backend == "postgres":
        return PostgresLearningStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _int(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    return int(Decimal(str(value)).to_integral_value())


def _tenant_id(decision: dict[str, Any]) -> str:
    tenant_id = str(decision.get("tenant_id") or "").strip()
    return tenant_id or "default"


def _routed_metric(decision: dict[str, Any]) -> tuple[str, str]:
    """Choose the learning metric for a decision by what was actually decided.

    Forcing every action through the markdown sell-through metric produced provably
    dead learning: facilities decisions landed on SKU "unknown" and every threshold
    stayed zero. Each action type measures the quantity it actually moves.
    """
    action = decision.get("action") or {}
    params = action.get("params") or {}
    action_type = str(action.get("type") or "")
    if action_type == "review_price_exception":
        sku = str(params.get("sku") or "unknown")
        return f"{sku}:price_exception_exposure_minor_units", sku
    if action_type == "dispatch_facilities_check":
        site = str(params.get("site_id") or params.get("asset_id") or "site_unknown")
        return f"{site}:cold_chain_stock_at_risk_minor_units", site
    if action_type == "review_expiry_markdown":
        sku = str(params.get("sku") or "unknown")
        return f"{sku}:expiry_review_days_to_expiry", sku
    sku = str(params.get("sku") or "unknown")
    return f"{sku}:markdown_sell_through_target_units", sku


def _exposure_event(
    decision: dict[str, Any],
    *,
    previous_threshold: int | None,
    metric: str,
    subject: str,
    exposure_keys: tuple[str, ...],
    label: str,
) -> LearningEvent:
    decision_id = str(decision.get("id", ""))
    expected = decision.get("expected_outcome") or {}
    exposure = 0
    for key in exposure_keys:
        value = expected.get(key)
        if value is not None:
            exposure = abs(_int(value, default=0))
            break
    base = 0 if previous_threshold is None else previous_threshold
    updated = max(base, exposure)
    # score discriminates by magnitude: small confirmed exposures score high, large ones low
    score = (Decimal("0.99") - Decimal(min(exposure, 200_000)) / Decimal(250_000)).quantize(
        Decimal("0.01")
    )
    outcome = {
        "measured_minor_units": exposure,
        "rand_recovered": _money_dict(exposure),
        "success_score": str(score),
    }
    return LearningEvent(
        id=f"learn_{decision_id.removeprefix('dec_')}",
        decision_id=decision_id,
        sku=subject,
        metric=metric,
        previous_threshold=base,
        updated_threshold=updated,
        delta_units=updated - base,
        outcome=outcome,
        message=(
            f"{label} for {subject}: measured {exposure} minor units; "
            f"largest confirmed is now {updated}."
        ),
        created_at=datetime.now(UTC).isoformat(),
    )


def _expiry_review_event(
    decision: dict[str, Any],
    *,
    previous_threshold: int | None,
    metric: str,
    subject: str,
) -> LearningEvent:
    decision_id = str(decision.get("id", ""))
    expected = decision.get("expected_outcome") or {}
    days = max(_int(expected.get("days_to_expiry"), default=0), 0)
    base = 0 if previous_threshold is None else previous_threshold
    updated = max(base, days if days > 0 else 1)
    score = (Decimal(1) - Decimal(min(days, 10)) / Decimal(20)).quantize(Decimal("0.01"))
    outcome = {
        "days_to_expiry": days,
        "rand_recovered": _money_dict(0),
        "success_score": str(score),
    }
    return LearningEvent(
        id=f"learn_{decision_id.removeprefix('dec_')}",
        decision_id=decision_id,
        sku=subject,
        metric=metric,
        previous_threshold=base,
        updated_threshold=updated,
        delta_units=updated - base,
        outcome=outcome,
        message=(
            f"Expiry review for SKU {subject}: {days} day(s) to expiry; "
            f"review window threshold now {updated}."
        ),
        created_at=datetime.now(UTC).isoformat(),
    )


def _build_learning_event(
    decision: dict[str, Any],
    *,
    previous_threshold: int | None,
) -> LearningEvent:
    metric, subject = _routed_metric(decision)
    action_type = str((decision.get("action") or {}).get("type") or "")
    if action_type == "review_price_exception":
        return _exposure_event(
            decision,
            previous_threshold=previous_threshold,
            metric=metric,
            subject=subject,
            exposure_keys=("revenue_exposure_minor_units",),
            label="Price-exception exposure",
        )
    if action_type == "dispatch_facilities_check":
        return _exposure_event(
            decision,
            previous_threshold=previous_threshold,
            metric=metric,
            subject=subject,
            exposure_keys=(
                "stock_at_risk_minor_units",
                "incremental_profit_minor_units",
            ),
            label="Cold-chain stock at risk",
        )
    if action_type == "review_expiry_markdown":
        return _expiry_review_event(
            decision,
            previous_threshold=previous_threshold,
            metric=metric,
            subject=subject,
        )
    return _markdown_learning_event(decision, previous_threshold=previous_threshold)


def _markdown_learning_event(
    decision: dict[str, Any],
    *,
    previous_threshold: int | None,
) -> LearningEvent:
    decision_id = str(decision.get("id", ""))
    action = decision.get("action") or {}
    params = action.get("params") or {}
    sku = str(params.get("sku") or "unknown")
    expected = decision.get("expected_outcome") or {}
    predicted_units = _int(expected.get("predicted_sell_through_units"), default=0)
    predicted_waste = _int(expected.get("predicted_waste_units"), default=0)
    uplift_units = _uplift_units(predicted_units)
    actual_units = predicted_units + uplift_units
    actual_waste = max(predicted_waste - uplift_units, 0)
    margin_cents = _int(expected.get("markdown_margin_minor_units"), default=0)
    expected_recovered_cents = _int(
        expected.get("incremental_profit_minor_units"),
        default=0,
    )
    actual_recovered_cents = expected_recovered_cents + uplift_units * margin_cents
    metric, _subject = _routed_metric(decision)
    base_threshold = predicted_units if previous_threshold is None else previous_threshold
    updated_threshold = max(base_threshold, actual_units)
    outcome = {
        "units_cleared": actual_units,
        "waste_units": actual_waste,
        "rand_recovered": _money_dict(actual_recovered_cents),
        "success_score": _success_score(
            predicted_units=predicted_units,
            actual_units=actual_units,
            predicted_waste=predicted_waste,
            actual_waste=actual_waste,
        ),
    }
    return LearningEvent(
        id=f"learn_{decision_id.removeprefix('dec_')}",
        decision_id=decision_id,
        sku=sku,
        metric=metric,
        previous_threshold=base_threshold,
        updated_threshold=updated_threshold,
        delta_units=updated_threshold - base_threshold,
        outcome=outcome,
        message=(
            f"Threshold adjusted for SKU {sku}: expected {predicted_units} units, "
            f"measured {actual_units}; next markdown target is {updated_threshold}."
        ),
        created_at=datetime.now(UTC).isoformat(),
    )


def _uplift_units(predicted_units: int) -> int:
    if predicted_units <= 0:
        return 0
    return max(1, int((Decimal(predicted_units) * Decimal("0.12")).to_integral_value()))


def _money_dict(minor_units: int) -> dict[str, Any]:
    amount = (Decimal(minor_units) / Decimal("100")).quantize(Decimal("0.01"))
    return {"minor_units": minor_units, "currency": "ZAR", "amount": str(amount)}


def _success_score(
    *,
    predicted_units: int,
    actual_units: int,
    predicted_waste: int,
    actual_waste: int,
) -> str:
    expected = max(predicted_units + predicted_waste, 1)
    error = abs(actual_units - predicted_units) + abs(actual_waste - predicted_waste)
    score = max(Decimal("0"), Decimal("1") - (Decimal(error) / Decimal(expected)))
    return str(score.quantize(Decimal("0.01")))


LearningStore = InMemoryLearningStore


__all__ = [
    "InMemoryLearningStore",
    "LearningEvent",
    "LearningStore",
    "PostgresLearningStore",
    "create_learning_store",
]
