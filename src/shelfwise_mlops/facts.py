from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_runtime import DataDomain, normalize_domain
from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage import now_iso as _now
from shelfwise_storage.rls import apply_tenant_rls

from .memory_consolidation import TenantFact

_PATTERN_TYPE = "tenant_fact"


class InMemoryTenantFactStore:
    """Process-local governed memory store for consolidated tenant facts."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._facts: dict[str, dict[str, Any]] = {}

    def record_many(self, facts: list[TenantFact]) -> list[dict[str, Any]]:
        persisted = [self.record(fact) for fact in facts]
        return persisted

    def record(self, fact: TenantFact) -> dict[str, Any]:
        now = _now()
        payload = fact.to_dict()
        payload.setdefault("active", True)
        payload.setdefault("pattern_type", _PATTERN_TYPE)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        with self._lock:
            self._facts[fact.id] = payload
            return deepcopy(payload)

    def tombstone(self, fact_id: str, *, tenant_id: str, reason: str) -> dict[str, Any] | None:
        now = _now()
        with self._lock:
            current = self._facts.get(fact_id)
            if current is None or current.get("tenant_id") != tenant_id:
                return None
            updated = deepcopy(current)
            updated["active"] = False
            updated["tombstone_reason"] = reason
            updated["tombstoned_at"] = now
            updated["updated_at"] = now
            self._facts[fact_id] = updated
            return deepcopy(updated)

    def list(
        self,
        *,
        tenant_id: str | None = None,
        data_domain: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            facts = list(self._facts.values())
        if tenant_id is not None:
            facts = [fact for fact in facts if fact.get("tenant_id") == tenant_id]
        if data_domain is not None:
            resolved_domain = normalize_domain(
                data_domain,
                default=DataDomain.OPERATIONAL_TWIN,
            )
            facts = [fact for fact in facts if fact.get("data_domain") == resolved_domain]
        if active_only:
            facts = [fact for fact in facts if fact.get("active", True) is True]
        facts.sort(key=lambda fact: (str(fact.get("sku", "")), str(fact.get("action", ""))))
        return [deepcopy(fact) for fact in facts]

    def clear(self) -> None:
        with self._lock:
            self._facts.clear()


class PostgresTenantFactStore:
    """Postgres-backed governed memory store using shelfwise_learned_patterns."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresTenantFactStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record_many(self, facts: list[TenantFact]) -> list[dict[str, Any]]:
        return [self.record(fact) for fact in facts]

    def record(self, fact: TenantFact) -> dict[str, Any]:
        now = _now()
        payload = fact.to_dict()
        payload.setdefault("active", True)
        payload.setdefault("pattern_type", _PATTERN_TYPE)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        with self._connect() as conn:
            conn.execute(
                """
                insert into shelfwise_learned_patterns
                    (
                        id, tenant_id, data_domain, pattern_type, sku, conclusion,
                        evidence_refs, payload, created_at
                    )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do update
                set tenant_id = excluded.tenant_id,
                    data_domain = excluded.data_domain,
                    pattern_type = excluded.pattern_type,
                    sku = excluded.sku,
                    conclusion = excluded.conclusion,
                    evidence_refs = excluded.evidence_refs,
                    payload = excluded.payload
                """,
                (
                    fact.id,
                    fact.tenant_id,
                    fact.data_domain,
                    _PATTERN_TYPE,
                    fact.sku,
                    fact.fact,
                    list(fact.evidence_refs),
                    jsonb(payload),
                    payload["created_at"],
                ),
            )
            conn.commit()
        return self.get(fact.id) or payload

    def get(self, fact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload
                from shelfwise_learned_patterns
                where id = %s and pattern_type = %s
                """,
                (fact_id, _PATTERN_TYPE),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def tombstone(self, fact_id: str, *, tenant_id: str, reason: str) -> dict[str, Any] | None:
        current = self.get(fact_id)
        if current is None or current.get("tenant_id") != tenant_id:
            return None
        now = _now()
        updated = deepcopy(current)
        updated["active"] = False
        updated["tombstone_reason"] = reason
        updated["tombstoned_at"] = now
        updated["updated_at"] = now
        with self._connect() as conn:
            conn.execute(
                """
                update shelfwise_learned_patterns
                set payload = %s
                where id = %s and tenant_id = %s and pattern_type = %s
                """,
                (jsonb(updated), fact_id, tenant_id, _PATTERN_TYPE),
            )
            conn.commit()
        return updated

    def list(
        self,
        *,
        tenant_id: str | None = None,
        data_domain: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        where = "where pattern_type = %s"
        params: list[Any] = [_PATTERN_TYPE]
        if tenant_id is not None:
            where += " and tenant_id = %s"
            params.append(tenant_id)
        if data_domain is not None:
            where += " and data_domain = %s"
            params.append(
                normalize_domain(data_domain, default=DataDomain.OPERATIONAL_TWIN)
            )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select payload
                from shelfwise_learned_patterns
                {where}
                order by sku, id
                """,
                tuple(params),
            ).fetchall()
        facts = [deepcopy(row["payload"]) for row in rows]
        if active_only:
            facts = [fact for fact in facts if fact.get("active", True) is True]
        return facts

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "delete from shelfwise_learned_patterns where pattern_type = %s",
                (_PATTERN_TYPE,),
            )
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_TENANT_FACT_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_learned_patterns",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def create_tenant_fact_store() -> InMemoryTenantFactStore | PostgresTenantFactStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryTenantFactStore()
    if backend == "postgres":
        return PostgresTenantFactStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


_TENANT_FACT_SCHEMA_SQL = """
create extension if not exists vector;
create table if not exists shelfwise_learned_patterns (
    id text primary key,
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    pattern_type text not null,
    sku text,
    conclusion text not null,
    evidence_refs text[] not null default '{}',
    payload jsonb not null,
    embedding vector(768),
    created_at timestamptz not null
);
alter table shelfwise_learned_patterns
add column if not exists data_domain text not null default 'world_simulation';
drop index if exists idx_shelfwise_learned_patterns_tenant_type;
create index if not exists idx_shelfwise_learned_patterns_tenant_type
on shelfwise_learned_patterns (tenant_id, data_domain, pattern_type);
"""
