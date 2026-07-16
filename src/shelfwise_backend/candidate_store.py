"""Tenant-scoped candidate observation and suppression storage."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage import validate_limit as _validate_limit
from shelfwise_storage.rls import apply_tenant_rls

from .candidate_factory import FleetCandidate
from .candidate_history import (
    CandidateHistoryEntry,
    CandidateHistoryStore,
    InMemoryCandidateHistoryStore,
    PostgresCandidateHistoryStore,
)

CANDIDATE_STATUSES = frozenset(
    {"new", "monitoring", "suppressed", "pending", "approved", "rejected", "resolved"}
)
TERMINAL_CANDIDATE_STATUSES = frozenset({"approved", "rejected", "resolved"})


class InMemoryCandidateStore:
    """Process-local candidate store used by the default zero-config runtime."""

    def __init__(self, *, history: CandidateHistoryStore | None = None) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._history = history if history is not None else InMemoryCandidateHistoryStore()

    def upsert_many(
        self, candidates: list[FleetCandidate], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Record observations idempotently and return the current lifecycle records."""
        return [self.upsert(candidate, now=now) for candidate in candidates]

    def upsert(self, candidate: FleetCandidate, *, now: datetime | None = None) -> dict[str, Any]:
        """Refresh one candidate without reopening terminal decisions."""
        timestamp = _timestamp(now)
        key = (candidate.tenant_id, candidate.candidate_key)
        with self._lock:
            existing = self._records.get(key)
            record = _base_record(candidate, timestamp)
            reason = "observed"
            if existing is not None:
                record = _merge_observation(existing, record, timestamp)
                reason = None if existing["status"] == record["status"] else "status_changed"
            self._records[key] = record
            if reason is not None:
                self._history.record(record, reason=reason)
            return deepcopy(record)

    def history(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[CandidateHistoryEntry]:
        """Return the immutable lifecycle transitions recorded for one candidate."""
        return self._history.list(
            tenant_id, candidate_key, since=since, until=until, limit=limit
        )

    def get(self, tenant_id: str, candidate_key: str) -> dict[str, Any] | None:
        """Return one candidate only when it belongs to the requested tenant."""
        with self._lock:
            record = self._records.get((tenant_id, candidate_key))
            return deepcopy(record) if record is not None else None

    def suppress(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        reason: str,
        until: datetime,
    ) -> dict[str, Any] | None:
        """Suppress an observed candidate until an explicit expiry time."""
        if not reason.strip():
            raise ValueError("suppression reason is required")
        with self._lock:
            record = self._records.get((tenant_id, candidate_key))
            if record is None:
                return None
            if record["status"] in TERMINAL_CANDIDATE_STATUSES or record.get("decision_id"):
                return deepcopy(record)
            updated = deepcopy(record)
            updated.update(
                {
                    "status": "suppressed",
                    "suppression_reason": reason.strip(),
                    "suppressed_until": _timestamp(until),
                    "updated_at": _timestamp(None),
                }
            )
            self._records[(tenant_id, candidate_key)] = updated
            self._history.record(updated, reason="suppressed")
            return deepcopy(updated)

    def link_decision(
        self, tenant_id: str, candidate_key: str, decision_id: str
    ) -> dict[str, Any] | None:
        """Link a promoted candidate to its existing HITL decision."""
        if not decision_id.strip():
            raise ValueError("decision_id is required")
        with self._lock:
            record = self._records.get((tenant_id, candidate_key))
            if record is None:
                return None
            if record["status"] in TERMINAL_CANDIDATE_STATUSES:
                return deepcopy(record)
            if record.get("decision_id") and record["decision_id"] != decision_id:
                return deepcopy(record)
            if record.get("decision_id") == decision_id:
                return deepcopy(record)
            updated = deepcopy(record)
            updated["decision_id"] = decision_id
            updated["status"] = "pending"
            updated["updated_at"] = _timestamp(None)
            self._records[(tenant_id, candidate_key)] = updated
            self._history.record(updated, reason="linked_decision")
            return deepcopy(updated)

    def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        data_domain: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List bounded candidates for one tenant, newest observations first."""
        _validate_status(status)
        _validate_limit(limit)
        with self._lock:
            records = [
                deepcopy(record)
                for (record_tenant, _), record in self._records.items()
                if record_tenant == tenant_id and (status is None or record["status"] == status)
                and (data_domain is None or record["data_domain"] == data_domain)
            ]
        return sorted(records, key=lambda item: item["updated_at"], reverse=True)[:limit]

    def clear(self) -> None:
        """Clear process-local records for tests and disposable runs."""
        with self._lock:
            self._records.clear()
        self._history.clear()


class PostgresCandidateStore:
    """Durable candidate store protected by the same tenant RLS contract as decisions."""

    def __init__(
        self, database_url: str, *, history: CandidateHistoryStore | None = None
    ) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresCandidateStore")
        self._database_url = database_url
        self._history = (
            history if history is not None else PostgresCandidateHistoryStore(database_url)
        )
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert_many(
        self, candidates: list[FleetCandidate], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Persist a bounded observation batch in one connection."""
        return [self.upsert(candidate, now=now) for candidate in candidates]

    def upsert(self, candidate: FleetCandidate, *, now: datetime | None = None) -> dict[str, Any]:
        """Insert or refresh a candidate while preserving lifecycle state."""
        timestamp = _timestamp(now)
        existing = self.get(candidate.tenant_id, candidate.candidate_key)
        record = _base_record(candidate, timestamp)
        reason = "observed"
        if existing is not None:
            record = _merge_observation(existing, record, timestamp)
            reason = None if existing["status"] == record["status"] else "status_changed"
        with self._connect(candidate.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_candidates
                    (candidate_key, tenant_id, data_domain, candidate_type, sku, lot_id, status,
                     score, urgency, exposure_units, monitoring_only, evidence,
                     first_seen_at, last_seen_at, updated_at, suppression_reason,
                     suppressed_until, decision_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s)
                on conflict (candidate_key) do update set
                    candidate_type = excluded.candidate_type,
                    sku = excluded.sku,
                    lot_id = excluded.lot_id,
                    status = excluded.status,
                    score = excluded.score,
                    urgency = excluded.urgency,
                    exposure_units = excluded.exposure_units,
                    monitoring_only = excluded.monitoring_only,
                    evidence = excluded.evidence,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    suppression_reason = excluded.suppression_reason,
                    suppressed_until = excluded.suppressed_until,
                    decision_id = excluded.decision_id
                """,
                _row_values(record),
            )
            conn.commit()
        if reason is not None:
            self._history.record(record, reason=reason)
        return record

    def history(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[CandidateHistoryEntry]:
        """Return the immutable lifecycle transitions recorded for one candidate."""
        return self._history.list(
            tenant_id, candidate_key, since=since, until=until, limit=limit
        )

    def get(self, tenant_id: str, candidate_key: str) -> dict[str, Any] | None:
        """Read one candidate under the active tenant RLS context."""
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                "select * from shelfwise_candidates where tenant_id = %s and candidate_key = %s",
                (tenant_id, candidate_key),
            ).fetchone()
        return _record_from_row(row) if row else None

    def suppress(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        reason: str,
        until: datetime,
    ) -> dict[str, Any] | None:
        """Persist a time-bounded suppression without reopening terminal records."""
        if not reason.strip():
            raise ValueError("suppression reason is required")
        current = self.get(tenant_id, candidate_key)
        if (
            current is None
            or current["status"] in TERMINAL_CANDIDATE_STATUSES
            or current.get("decision_id")
        ):
            return current
        current.update(
            {
                "status": "suppressed",
                "suppression_reason": reason.strip(),
                "suppressed_until": _timestamp(until),
                "updated_at": _timestamp(None),
            }
        )
        return self._save(current, reason="suppressed")

    def link_decision(
        self, tenant_id: str, candidate_key: str, decision_id: str
    ) -> dict[str, Any] | None:
        """Link a candidate to an existing decision without duplicating it."""
        if not decision_id.strip():
            raise ValueError("decision_id is required")
        current = self.get(tenant_id, candidate_key)
        if current is None:
            return None
        if current["status"] in TERMINAL_CANDIDATE_STATUSES:
            return current
        if current.get("decision_id"):
            return current
        current.update(
            {"decision_id": decision_id, "status": "pending", "updated_at": _timestamp(None)}
        )
        return self._save(current, reason="linked_decision")

    def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        data_domain: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List bounded, tenant-scoped candidate records."""
        _validate_status(status)
        _validate_limit(limit)
        clauses: list[str] = []
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("status = %s")
            params.append(status)
        if data_domain:
            clauses.append("data_domain = %s")
            params.append(data_domain)
        clause = "and " + " and ".join(clauses) if clauses else ""
        params.append(limit)
        query = (
            "select * from shelfwise_candidates where tenant_id = %s "
            f"{clause} order by updated_at desc limit %s"
        )
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_record_from_row(row) for row in rows]

    def clear(self) -> None:
        """Clear candidates visible to the active tenant."""
        with self._connect() as conn:
            conn.execute("delete from shelfwise_candidates")
            conn.commit()
        self._history.clear()

    def _save(self, record: dict[str, Any], *, reason: str) -> dict[str, Any]:
        with self._connect(record["tenant_id"]) as conn:
            conn.execute(
                """
                update shelfwise_candidates
                set status = %s, evidence = %s, updated_at = %s,
                    suppression_reason = %s, suppressed_until = %s, decision_id = %s
                where tenant_id = %s and candidate_key = %s
                """,
                (
                    record["status"],
                    jsonb(record["evidence"]),
                    record["updated_at"],
                    record["suppression_reason"],
                    record["suppressed_until"],
                    record["decision_id"],
                    record["tenant_id"],
                    record["candidate_key"],
                ),
            )
            conn.commit()
        self._history.record(record, reason=reason)
        return record

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CANDIDATE_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_candidates",))
            conn.commit()

    def _connect(self, tenant_id: str | None = None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_candidate_store() -> InMemoryCandidateStore | PostgresCandidateStore:
    """Create the candidate store using the existing ShelfWise storage switch."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryCandidateStore()
    if backend == "postgres":
        return PostgresCandidateStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _base_record(candidate: FleetCandidate, timestamp: str) -> dict[str, Any]:
    return {
        **candidate.to_dict(),
        "status": "monitoring" if candidate.monitoring_only else "new",
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "updated_at": timestamp,
        "suppression_reason": None,
        "suppressed_until": None,
        "decision_id": None,
    }


def _merge_observation(
    existing: dict[str, Any], fresh: dict[str, Any], timestamp: str
) -> dict[str, Any]:
    status = str(existing.get("status") or "new")
    suppression_until = existing.get("suppressed_until")
    if status == "suppressed" and suppression_until and suppression_until <= timestamp:
        status = "monitoring" if fresh["monitoring_only"] else "new"
        suppression_until = None
        existing = {**existing, "suppression_reason": None}
    if status in TERMINAL_CANDIDATE_STATUSES:
        fresh["status"] = status
    else:
        fresh["status"] = status
    fresh["first_seen_at"] = existing.get("first_seen_at", timestamp)
    fresh["last_seen_at"] = timestamp
    fresh["suppression_reason"] = existing.get("suppression_reason")
    fresh["suppressed_until"] = suppression_until
    fresh["decision_id"] = existing.get("decision_id")
    return fresh


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _validate_status(status: str | None) -> None:
    if status is not None and status not in CANDIDATE_STATUSES:
        raise ValueError(f"unsupported candidate status: {status}")


def _row_values(record: dict[str, Any]) -> tuple[Any, ...]:
    values = tuple(
        record[key]
        for key in (
            "candidate_key",
            "tenant_id",
            "data_domain",
            "candidate_type",
            "sku",
            "lot_id",
            "status",
            "score",
            "urgency",
            "exposure_units",
            "monitoring_only",
        )
    )
    return (
        *values,
        jsonb(record["evidence"]),
        record["first_seen_at"],
        record["last_seen_at"],
        record["updated_at"],
        record["suppression_reason"],
        record["suppressed_until"],
        record["decision_id"],
    )


def _record_from_row(row: Any) -> dict[str, Any]:
    record = dict(row)
    for key in ("first_seen_at", "last_seen_at", "updated_at", "suppressed_until"):
        if record.get(key) is not None and hasattr(record[key], "isoformat"):
            record[key] = record[key].isoformat()
    return record


_CANDIDATE_SCHEMA_SQL = """
create table if not exists shelfwise_candidates (
    candidate_key text primary key,
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    candidate_type text not null,
    sku text not null,
    lot_id text,
    status text not null,
    score numeric not null,
    urgency numeric not null,
    exposure_units integer not null,
    monitoring_only boolean not null,
    evidence jsonb not null,
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    updated_at timestamptz not null,
    suppression_reason text,
    suppressed_until timestamptz,
    decision_id text
);
alter table shelfwise_candidates
add column if not exists data_domain text not null default 'world_simulation';
drop index if exists idx_shelfwise_candidates_tenant_status_updated;
create index if not exists idx_shelfwise_candidates_tenant_status_updated
on shelfwise_candidates (tenant_id, data_domain, status, updated_at desc);
drop index if exists idx_shelfwise_candidates_tenant_suppression;
create index if not exists idx_shelfwise_candidates_tenant_suppression
on shelfwise_candidates (tenant_id, data_domain, suppressed_until);
"""
