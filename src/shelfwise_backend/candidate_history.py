"""Append-only lifecycle history for fleet candidates.

`CandidateStore.upsert`/`suppress`/`link_decision` mutate one current-state row per
candidate - that row alone cannot answer "how did this candidate's status change over
time" once an operator asks why a candidate was suppressed, or how long it sat in
`monitoring` before promotion. This module keeps one immutable entry per real lifecycle
transition, mirroring the append-only observation pattern already used by
`shelfwise_twin`/`shelfwise_connectors.inbound_store` rather than inventing a new shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol

from shelfwise_storage import auto_schema_enabled, connect
from shelfwise_storage import validate_limit as _validate_limit
from shelfwise_storage.rls import apply_tenant_rls

MAX_HISTORY_LIMIT = 500


@dataclass(frozen=True, slots=True)
class CandidateHistoryEntry:
    """One immutable lifecycle transition for a candidate."""

    tenant_id: str
    data_domain: str
    candidate_key: str
    sequence: int
    reason: str
    status: str
    score: float
    urgency: float
    exposure_units: int
    decision_id: str | None
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "data_domain": self.data_domain,
            "candidate_key": self.candidate_key,
            "sequence": self.sequence,
            "reason": self.reason,
            "status": self.status,
            "score": self.score,
            "urgency": self.urgency,
            "exposure_units": self.exposure_units,
            "decision_id": self.decision_id,
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(),
        }


class CandidateHistoryStore(Protocol):
    """Storage contract shared by the disposable local and durable Postgres runtimes."""

    def record(self, record: dict[str, Any], *, reason: str) -> CandidateHistoryEntry: ...

    def list(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[CandidateHistoryEntry]: ...

    def clear(self) -> None: ...


class InMemoryCandidateHistoryStore:
    """Process-local candidate history used by the default zero-config runtime."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[tuple[str, str], list[CandidateHistoryEntry]] = {}

    def record(self, record: dict[str, Any], *, reason: str) -> CandidateHistoryEntry:
        key = (record["tenant_id"], record["candidate_key"])
        with self._lock:
            existing = self._entries.setdefault(key, [])
            entry = CandidateHistoryEntry(
                tenant_id=record["tenant_id"],
                data_domain=record["data_domain"],
                candidate_key=record["candidate_key"],
                sequence=len(existing) + 1,
                reason=reason,
                status=record["status"],
                score=float(record["score"]),
                urgency=float(record["urgency"]),
                exposure_units=int(record["exposure_units"]),
                decision_id=record.get("decision_id"),
                recorded_at=datetime.now(UTC),
            )
            existing.append(entry)
            return entry

    def list(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[CandidateHistoryEntry]:
        _validate_limit(limit, max_limit=MAX_HISTORY_LIMIT)
        with self._lock:
            entries = list(self._entries.get((tenant_id, candidate_key), ()))
        if since is not None:
            entries = [e for e in entries if e.recorded_at >= since]
        if until is not None:
            entries = [e for e in entries if e.recorded_at <= until]
        return sorted(entries, key=lambda e: e.sequence, reverse=True)[:limit]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class PostgresCandidateHistoryStore:
    """Durable candidate history protected by the same tenant RLS contract as candidates."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresCandidateHistoryStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(self, record: dict[str, Any], *, reason: str) -> CandidateHistoryEntry:
        tenant_id = record["tenant_id"]
        candidate_key = record["candidate_key"]
        recorded_at = datetime.now(UTC)
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_candidate_history
                    (tenant_id, data_domain, candidate_key, sequence, reason, status,
                     score, urgency, exposure_units, decision_id, recorded_at)
                values (
                    %s, %s, %s,
                    coalesce(
                        (select max(sequence) + 1 from shelfwise_candidate_history
                         where tenant_id = %s and candidate_key = %s),
                        1
                    ),
                    %s, %s, %s, %s, %s, %s, %s
                )
                returning sequence
                """,
                (
                    tenant_id, record["data_domain"], candidate_key,
                    tenant_id, candidate_key,
                    reason, record["status"], record["score"], record["urgency"],
                    record["exposure_units"], record.get("decision_id"), recorded_at,
                ),
            ).fetchone()
            conn.commit()
        return CandidateHistoryEntry(
            tenant_id=tenant_id,
            data_domain=record["data_domain"],
            candidate_key=candidate_key,
            sequence=int(row["sequence"]),
            reason=reason,
            status=record["status"],
            score=float(record["score"]),
            urgency=float(record["urgency"]),
            exposure_units=int(record["exposure_units"]),
            decision_id=record.get("decision_id"),
            recorded_at=recorded_at,
        )

    def list(
        self,
        tenant_id: str,
        candidate_key: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[CandidateHistoryEntry]:
        _validate_limit(limit, max_limit=MAX_HISTORY_LIMIT)
        clauses = ["tenant_id = %s", "candidate_key = %s"]
        params: list[Any] = [tenant_id, candidate_key]
        if since is not None:
            clauses.append("recorded_at >= %s")
            params.append(since)
        if until is not None:
            clauses.append("recorded_at <= %s")
            params.append(until)
        params.append(limit)
        query = (
            "select * from shelfwise_candidate_history where "
            + " and ".join(clauses)
            + " order by sequence desc limit %s"
        )
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_entry_from_row(row) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_candidate_history")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_CANDIDATE_HISTORY_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_candidate_history",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def _entry_from_row(row: Any) -> CandidateHistoryEntry:
    recorded_at = row["recorded_at"]
    return CandidateHistoryEntry(
        tenant_id=row["tenant_id"],
        data_domain=row["data_domain"],
        candidate_key=row["candidate_key"],
        sequence=int(row["sequence"]),
        reason=row["reason"],
        status=row["status"],
        score=float(row["score"]),
        urgency=float(row["urgency"]),
        exposure_units=int(row["exposure_units"]),
        decision_id=row["decision_id"],
        recorded_at=recorded_at,
    )


_CANDIDATE_HISTORY_SCHEMA_SQL = """
create table if not exists shelfwise_candidate_history (
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    candidate_key text not null,
    sequence integer not null,
    reason text not null,
    status text not null,
    score numeric not null,
    urgency numeric not null,
    exposure_units integer not null,
    decision_id text,
    recorded_at timestamptz not null,
    primary key (tenant_id, candidate_key, sequence)
);
create index if not exists idx_shelfwise_candidate_history_tenant_key
on shelfwise_candidate_history (tenant_id, candidate_key, sequence desc);
"""
