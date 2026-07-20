from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage import now_iso as _now
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryDecisionStore:
    """Process-local HITL decision store used by the default zero-config runtime.

    Production runs `PostgresDecisionStore` below (selected via
    SHELFWISE_STORE_BACKEND=postgres); this class keeps the identical approval-loop
    contract - deterministic, idempotent for repeat clicks, terminal states protected -
    without any external dependency for local development and tests.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._decisions: dict[str, dict[str, Any]] = {}

    def upsert(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        with self._lock:
            existing = self._decisions.get(decision_id)
            if existing and existing.get("status") in {"approved", "rejected"}:
                return deepcopy(existing)

            record = deepcopy(existing or {})
            record.update(deepcopy(decision))
            record.setdefault("data_domain", "world_simulation")
            record.setdefault("created_at", _now())
            record.setdefault("updated_at", record["created_at"])
            record.setdefault("review", None)
            self._decisions[decision_id] = record
            return deepcopy(record)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item) for item in self._decisions.values()]

    def clear(self) -> None:
        """Reset to empty. Used between test runs - decision ids are stable per scenario now
        (see shelfwise_backend.cascade), so a real deploy never needs this at runtime."""
        with self._lock:
            self._decisions.clear()

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._decisions.get(decision_id)
            return deepcopy(item) if item else None

    def annotate(self, decision_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            item = self._decisions.get(decision_id)
            if item is None:
                return None
            updated = deepcopy(item)
            updated.update(deepcopy(fields))
            updated["updated_at"] = _now()
            self._decisions[decision_id] = updated
            return deepcopy(updated)

    def approve(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "approved", reviewer)

    def reject(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "rejected", reviewer)

    def _transition(self, decision_id: str, status: str, reviewer: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._decisions.get(decision_id)
            if item is None:
                return None
            if item.get("status") in {"approved", "rejected"}:
                return deepcopy(item)
            updated = deepcopy(item)
            updated["status"] = status
            updated["updated_at"] = _now()
            updated["review"] = {
                "reviewer": reviewer,
                "status": status,
                "reviewed_at": updated["updated_at"],
            }
            self._decisions[decision_id] = updated
            return deepcopy(updated)


class PostgresDecisionStore:
    """Postgres-backed HITL decision store.

    The table stores the full API payload as jsonb so the current frontend/API contract can evolve
    without a migration for every evidence field, while status is duplicated into a column for fast
    lifecycle filtering and terminal-state protection.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresDecisionStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        existing = self.get(decision_id)
        if existing and existing.get("status") in {"approved", "rejected"}:
            return existing

        record = deepcopy(existing or {})
        record.update(deepcopy(decision))
        record.setdefault("data_domain", "world_simulation")
        tenant_id = _tenant_id(record)
        record.setdefault("created_at", _now())
        record.setdefault("updated_at", record["created_at"])
        record.setdefault("review", None)
        # Bind the RLS session to the record's own tenant, not whatever tenant (if any)
        # happens to be ambient on the calling context. The async worker already binds
        # per-event before calling upsert(), but the synchronous cascade fallback (used
        # whenever WORKER_ENABLED is off) has no such binding, so an unbound or
        # differently-scoped caller would otherwise have this write rejected outright by
        # the tenant_id RLS policy (found 2026-07-15 by running the real store against a
        # real least-privilege Postgres role instead of the always-permissive in-memory
        # fake).
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_decisions
                    (id, tenant_id, data_domain, status, payload, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do update
                set tenant_id = excluded.tenant_id,
                    data_domain = excluded.data_domain,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                where shelfwise_decisions.status not in ('approved', 'rejected')
                """,
                (
                    decision_id,
                    tenant_id,
                    str(record["data_domain"]),
                    str(record.get("status", "")),
                    jsonb(record),
                    record["created_at"],
                    record["updated_at"],
                ),
            )
            conn.commit()
        return self.get(decision_id) or deepcopy(record)

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select payload
                from shelfwise_decisions
                order by updated_at desc, created_at desc, id
                """
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_decisions")
            conn.commit()

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select payload from shelfwise_decisions where id = %s",
                (decision_id,),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def annotate(self, decision_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.get(decision_id)
        if current is None:
            return None
        updated = deepcopy(current)
        updated.update(deepcopy(fields))
        updated["updated_at"] = _now()
        return self._save_payload(decision_id, updated)

    def approve(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "approved", reviewer)

    def reject(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "rejected", reviewer)

    def _transition(self, decision_id: str, status: str, reviewer: str) -> dict[str, Any] | None:
        current = self.get(decision_id)
        if current is None:
            return None
        if current.get("status") in {"approved", "rejected"}:
            return current
        updated = deepcopy(current)
        updated["status"] = status
        updated["updated_at"] = _now()
        updated["review"] = {
            "reviewer": reviewer,
            "status": status,
            "reviewed_at": updated["updated_at"],
        }
        with self._connect() as conn:
            row = conn.execute(
                """
                update shelfwise_decisions
                set status = %s,
                    payload = %s,
                    updated_at = %s
                where id = %s and status = 'pending'
                returning payload
                """,
                (
                    status,
                    jsonb(updated),
                    updated["updated_at"],
                    decision_id,
                ),
            ).fetchone()
            conn.commit()
        if row is not None:
            payload = row["payload"]
            return deepcopy(payload if isinstance(payload, dict) else updated)
        return self.get(decision_id)

    def _save_payload(self, decision_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                update shelfwise_decisions
                set status = %s,
                    payload = %s,
                    updated_at = %s
                where id = %s
                """,
                (
                    str(payload.get("status", "")),
                    jsonb(payload),
                    payload["updated_at"],
                    decision_id,
                ),
            )
            conn.commit()
        return deepcopy(payload)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists shelfwise_decisions (
                    id text primary key,
                    tenant_id text not null default 'default',
                    data_domain text not null default 'world_simulation',
                    status text not null,
                    payload jsonb not null,
                    created_at timestamptz not null,
                    updated_at timestamptz not null
                )
                """
            )
            conn.execute(
                """
                alter table shelfwise_decisions
                add column if not exists tenant_id text not null default 'default'
                """
            )
            conn.execute(
                """
                alter table shelfwise_decisions
                add column if not exists data_domain text not null default 'world_simulation'
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_decisions_status_updated
                on shelfwise_decisions (status, updated_at desc)
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_decisions_tenant_updated
                on shelfwise_decisions (tenant_id, updated_at desc)
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_decisions_tenant_domain_updated
                on shelfwise_decisions (tenant_id, data_domain, updated_at desc)
                """
            )
            apply_tenant_rls(conn, ("shelfwise_decisions",))
            conn.commit()

    def _connect(self, tenant_id: str | None = None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_decision_store() -> InMemoryDecisionStore | PostgresDecisionStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryDecisionStore()
    if backend == "postgres":
        return PostgresDecisionStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


DecisionStore = InMemoryDecisionStore


def _tenant_id(decision: dict[str, Any]) -> str:
    tenant_id = str(decision.get("tenant_id") or "").strip()
    return tenant_id or "default"
