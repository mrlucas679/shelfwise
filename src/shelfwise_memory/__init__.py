from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
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
    tenant_id: str = "default"
    data_domain: str = "world_simulation"

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
            "tenant_id": self.tenant_id,
            "data_domain": self.data_domain,
        }


class InMemoryLearningStore:
    """Deterministic memory layer for the demo's visible learning moment."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._thresholds: dict[tuple[str, str, str], int] = {}
        self._events_by_decision: dict[tuple[str, str, str], LearningEvent] = {}

    def thresholds(
        self, tenant_id: str | None = None, data_domain: str | None = None
    ) -> dict[str, int]:
        with self._lock:
            return {
                metric: threshold
                for (event_tenant_id, event_domain, metric), threshold in self._thresholds.items()
                if tenant_id is None or event_tenant_id == tenant_id
                if data_domain is None or event_domain == data_domain
            }

    def list_events(
        self, tenant_id: str | None = None, data_domain: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                event.to_dict()
                for event in self._events_by_decision.values()
                if tenant_id is None or event.tenant_id == tenant_id
                if data_domain is None or event.data_domain == data_domain
            ]

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

        tenant_id = _tenant_id(decision)
        data_domain = _data_domain(decision)
        with self._lock:
            event_key = (tenant_id, data_domain, decision_id)
            existing = self._events_by_decision.get(event_key)
            if existing is not None:
                return existing.to_dict()

            metric, _subject = routed_metric(decision)
            event = _build_learning_event(
                decision,
                previous_threshold=self._thresholds.get((tenant_id, data_domain, metric)),
            )
            self._thresholds[(tenant_id, data_domain, metric)] = event.updated_threshold
            self._events_by_decision[event_key] = event
            return event.to_dict()

    def record_rejected_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Record a terminal rejection without moving an approval-derived threshold."""
        event = _build_rejection_event(decision)
        with self._lock:
            key = (event.tenant_id, event.data_domain, event.decision_id)
            existing = self._events_by_decision.get(key)
            if existing is not None:
                return existing.to_dict()
            self._events_by_decision[key] = event
        return event.to_dict()


class PostgresLearningStore:
    """Postgres-backed learning store for approved outcomes and threshold memory."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresLearningStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def thresholds(
        self, tenant_id: str | None = None, data_domain: str | None = None
    ) -> dict[str, int]:
        with self._connect() as conn:
            clauses: list[str] = []
            params: list[str] = []
            if tenant_id is not None:
                clauses.append("tenant_id = %s")
                params.append(tenant_id)
            if data_domain is not None:
                clauses.append("data_domain = %s")
                params.append(data_domain)
            where = " where " + " and ".join(clauses) if clauses else ""
            rows = conn.execute(
                "select metric, threshold_units from shelfwise_learning_thresholds" + where,
                tuple(params),
            ).fetchall()
        return {row["metric"]: int(row["threshold_units"]) for row in rows}

    def list_events(
        self, tenant_id: str | None = None, data_domain: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            clauses: list[str] = []
            params: list[str] = []
            if tenant_id is not None:
                clauses.append("tenant_id = %s")
                params.append(tenant_id)
            if data_domain is not None:
                clauses.append("data_domain = %s")
                params.append(data_domain)
            where = " where " + " and ".join(clauses) if clauses else ""
            rows = conn.execute(
                "select tenant_id, data_domain, payload from shelfwise_learning_events"
                + where
                + " order by created_at desc, decision_id",
                tuple(params),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = deepcopy(row["payload"])
            event["tenant_id"] = str(row["tenant_id"])
            event["data_domain"] = str(row["data_domain"])
            events.append(event)
        return events

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

        tenant_id = _tenant_id(decision)
        data_domain = _data_domain(decision)
        with self._connect() as conn:
            existing = conn.execute(
                """
                select payload
                from shelfwise_learning_events
                where tenant_id = %s and data_domain = %s and decision_id = %s
                """,
                (tenant_id, data_domain, decision_id),
            ).fetchone()
            if existing is not None:
                return deepcopy(existing["payload"])

            metric, _subject = routed_metric(decision)
            threshold_row = conn.execute(
                """
                select threshold_units
                from shelfwise_learning_thresholds
                where tenant_id = %s and data_domain = %s and metric = %s
                for update
                """,
                (tenant_id, data_domain, metric),
            ).fetchone()
            previous_threshold = (
                int(threshold_row["threshold_units"]) if threshold_row is not None else None
            )
            event = _build_learning_event(decision, previous_threshold=previous_threshold)
            payload = event.to_dict()
            conn.execute(
                """
                insert into shelfwise_learning_thresholds
                    (tenant_id, data_domain, metric, sku, threshold_units, updated_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, data_domain, metric) do update
                set threshold_units = greatest(
                        shelfwise_learning_thresholds.threshold_units,
                        excluded.threshold_units
                    ),
                    updated_at = greatest(
                        shelfwise_learning_thresholds.updated_at,
                        excluded.updated_at
                    )
                """,
                (
                    tenant_id,
                    data_domain,
                    event.metric,
                    event.sku,
                    event.updated_threshold,
                    event.created_at,
                ),
            )
            committed_threshold = conn.execute(
                """
                select threshold_units
                from shelfwise_learning_thresholds
                where tenant_id = %s and data_domain = %s and metric = %s
                """,
                (tenant_id, data_domain, event.metric),
            ).fetchone()
            if committed_threshold is None:
                raise RuntimeError("learning threshold was not persisted")
            final_threshold = int(committed_threshold["threshold_units"])
            if final_threshold != event.updated_threshold:
                payload["updated_threshold"] = final_threshold
                payload["delta_units"] = final_threshold - event.previous_threshold
                message_prefix = str(payload["message"]).split("largest confirmed is now", 1)[0]
                payload["message"] = f"{message_prefix}largest confirmed is now {final_threshold}."
            # Two concurrent approvals of the same decision can both pass the
            # existing-event check above (the decision-store transition lets the loser
            # through with the already-approved record), so the insert must absorb the
            # race at the database level instead of surfacing a unique-violation 500 to
            # a client whose approval actually succeeded. Both racers compute an
            # identical event from the same previous_threshold, so returning the
            # winner's persisted row is exact, not approximate.
            inserted = conn.execute(
                """
                insert into shelfwise_learning_events
                    (tenant_id, data_domain, decision_id, payload, created_at)
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id, data_domain, decision_id) do nothing
                returning payload
                """,
                (tenant_id, data_domain, event.decision_id, jsonb(payload), event.created_at),
            ).fetchone()
            if inserted is None:
                winner = conn.execute(
                    """
                    select payload
                    from shelfwise_learning_events
                    where tenant_id = %s and data_domain = %s and decision_id = %s
                    """,
                    (tenant_id, data_domain, event.decision_id),
                ).fetchone()
                conn.commit()
                return deepcopy(winner["payload"]) if winner else payload
            conn.commit()
            return payload

    def record_rejected_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Persist a terminal rejection outcome while leaving thresholds unchanged."""
        event = _build_rejection_event(decision)
        payload = event.to_dict()
        with self._connect() as conn:
            existing = conn.execute(
                """select payload from shelfwise_learning_events
                   where tenant_id = %s and data_domain = %s and decision_id = %s""",
                (event.tenant_id, event.data_domain, event.decision_id),
            ).fetchone()
            if existing is not None:
                return deepcopy(existing["payload"])
            # Same double-submit race as record_approved_decision: absorb the conflict
            # in the database rather than 500ing the losing (but equally valid) caller.
            inserted = conn.execute(
                """insert into shelfwise_learning_events
                   (tenant_id, data_domain, decision_id, payload, created_at)
                   values (%s, %s, %s, %s, %s)
                   on conflict (tenant_id, data_domain, decision_id) do nothing
                   returning payload""",
                (
                    event.tenant_id,
                    event.data_domain,
                    event.decision_id,
                    jsonb(payload),
                    event.created_at,
                ),
            ).fetchone()
            if inserted is None:
                winner = conn.execute(
                    """select payload from shelfwise_learning_events
                       where tenant_id = %s and data_domain = %s and decision_id = %s""",
                    (event.tenant_id, event.data_domain, event.decision_id),
                ).fetchone()
                conn.commit()
                return deepcopy(winner["payload"]) if winner else payload
            conn.commit()
        return payload

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists shelfwise_learning_thresholds (
                    tenant_id text not null default 'default',
                    data_domain text not null default 'world_simulation',
                    metric text not null,
                    sku text not null,
                    threshold_units integer not null,
                    updated_at timestamptz not null,
                    primary key (tenant_id, data_domain, metric)
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_learning_thresholds
                add column if not exists tenant_id text not null default 'default';
                alter table shelfwise_learning_thresholds
                add column if not exists data_domain text not null default 'world_simulation';
                alter table shelfwise_learning_thresholds
                drop constraint if exists shelfwise_learning_thresholds_pkey;
                alter table shelfwise_learning_thresholds
                add primary key (tenant_id, data_domain, metric)
                """
            )
            conn.execute(
                """
                drop index if exists ux_shelfwise_learning_thresholds_tenant_metric;
                create unique index if not exists ux_shelfwise_learning_thresholds_domain_metric
                on shelfwise_learning_thresholds (tenant_id, data_domain, metric)
                """
            )
            conn.execute(
                """
                create table if not exists shelfwise_learning_events (
                    tenant_id text not null default 'default',
                    data_domain text not null default 'world_simulation',
                    decision_id text not null,
                    payload jsonb not null,
                    created_at timestamptz not null,
                    primary key (tenant_id, data_domain, decision_id)
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_learning_events
                add column if not exists tenant_id text not null default 'default';
                alter table shelfwise_learning_events
                add column if not exists data_domain text not null default 'world_simulation';
                alter table shelfwise_learning_events
                drop constraint if exists shelfwise_learning_events_pkey;
                alter table shelfwise_learning_events
                add primary key (tenant_id, data_domain, decision_id)
                """
            )
            conn.execute(
                """
                drop index if exists idx_shelfwise_learning_events_tenant_created;
                create index if not exists idx_shelfwise_learning_events_tenant_created
                on shelfwise_learning_events (tenant_id, data_domain, created_at desc)
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


def _data_domain(decision: dict[str, Any]) -> str:
    domain = str(decision.get("data_domain") or "world_simulation").strip()
    if domain not in {"operational_twin", "world_simulation"}:
        raise ValueError("learning requires an operational or simulation decision domain")
    return domain


def routed_metric(decision: dict[str, Any]) -> tuple[str, str]:
    """Choose the learning metric for a decision by what was actually decided.

    Forcing every action through the markdown sell-through metric produced provably
    dead learning: facilities decisions landed on SKU "unknown" and every threshold
    stayed zero. Each action type measures the quantity it actually moves.

    Public (not `_`-prefixed): this is the one authoritative definition of a decision's
    metric key, shared by the write path here and by cascade builders that cite a prior
    threshold as evidence (`cascade.py`'s `_learned_threshold_evidence`) - both sides
    must agree on the same key or a cascade's citation would silently look up nothing.
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
    if action_type == "reorder":
        sku = str(params.get("sku") or "unknown")
        return f"{sku}:reorder_stockout_exposure_minor_units", sku
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
        tenant_id=_tenant_id(decision),
        data_domain=_data_domain(decision),
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
        tenant_id=_tenant_id(decision),
        data_domain=_data_domain(decision),
    )


def _build_learning_event(
    decision: dict[str, Any],
    *,
    previous_threshold: int | None,
) -> LearningEvent:
    metric, subject = routed_metric(decision)
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
    if action_type == "reorder":
        # Procurement decisions had no learning-metric route at all (not degraded, entirely
        # absent) in both the deterministic and agentic cascades - approving a reorder never
        # built organizational memory, so the app could never legitimately claim continuous
        # learning for procurement, one of the store positions the product is required to
        # cover for real, not as a demo slice. `stockout_exposure_minor_units` is the
        # deterministic cascade's existing real field (money at risk of stockout, avoided by
        # reordering) - reused here rather than inventing a new one, so both paths agree.
        return _exposure_event(
            decision,
            previous_threshold=previous_threshold,
            metric=metric,
            subject=subject,
            exposure_keys=(
                "stockout_exposure_minor_units",
                "incremental_profit_minor_units",
            ),
            label="Reorder stockout exposure avoided",
        )
    return _markdown_learning_event(decision, previous_threshold=previous_threshold)


def _build_rejection_event(decision: dict[str, Any]) -> LearningEvent:
    if decision.get("status") != "rejected":
        raise ValueError("rejection learning requires a rejected decision")
    decision_id = str(decision.get("id") or "")
    if not decision_id:
        raise ValueError("decision must include id")
    action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    subject = str(params.get("sku") or params.get("site_id") or "unknown")
    return LearningEvent(
        id=f"learn_{decision_id.removeprefix('dec_')}",
        decision_id=decision_id,
        sku=subject,
        metric=f"{subject}:rejection_outcome",
        previous_threshold=0,
        updated_threshold=0,
        delta_units=0,
        outcome={
            "decision_status": "rejected",
            "action_prevented": str(action.get("type") or "unknown"),
        },
        message=f"Rejected {action.get('type') or 'unknown'} for {subject}; no write-back created.",
        created_at=datetime.now(UTC).isoformat(),
        tenant_id=_tenant_id(decision),
        data_domain=_data_domain(decision),
    )


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
    metric, _subject = routed_metric(decision)
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
        tenant_id=_tenant_id(decision),
        data_domain=_data_domain(decision),
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
    "routed_metric",
]
