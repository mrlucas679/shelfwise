from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_storage import (
    auto_schema_enabled,
    connect,
    jsonb,
)
from shelfwise_storage import (
    now_iso as _now,
)
from shelfwise_storage import (
    validate_limit as _validate_limit,
)
from shelfwise_storage.rls import apply_tenant_rls


class InMemoryWorldgenRunStore:
    """Process-local ledger for synthetic virtual-store drill runs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def record(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id(run)
        now = _now()
        payload = deepcopy(run)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        with self._lock:
            self._runs[run_id] = payload
            return deepcopy(payload)

    def get(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or (tenant_id is not None and run.get("tenant_id") != tenant_id):
                return None
            return deepcopy(run)

    def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self._lock:
            runs = list(self._runs.values())
        if tenant_id is not None:
            runs = [run for run in runs if run.get("tenant_id") == tenant_id]
        runs.sort(key=lambda run: str(run.get("created_at", "")), reverse=True)
        return [deepcopy(run) for run in runs[:limit]]

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


class PostgresWorldgenRunStore:
    """Postgres-backed ledger for synthetic virtual-store drill runs."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresWorldgenRunStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id(run)
        tenant_id = str(run.get("tenant_id") or "default")
        scenario_id = str(run.get("scenario_id") or "")
        seed = int(run.get("seed") or 0)
        status = str(run.get("status") or "completed")
        now = _now()
        payload = deepcopy(run)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        with self._connect() as conn:
            conn.execute(
                """
                insert into shelfwise_worldgen_runs
                    (run_id, tenant_id, scenario_id, seed, status, payload, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id) do update
                set tenant_id = excluded.tenant_id,
                    scenario_id = excluded.scenario_id,
                    seed = excluded.seed,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    tenant_id,
                    scenario_id,
                    seed,
                    status,
                    jsonb(payload),
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()
        return self.get(run_id, tenant_id=tenant_id) or payload

    def get(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        where = "run_id = %s and tenant_id = %s" if tenant_id is not None else "run_id = %s"
        params = (run_id, tenant_id) if tenant_id is not None else (run_id,)
        with self._connect() as conn:
            row = conn.execute(
                f"select payload from shelfwise_worldgen_runs where {where}",
                params,
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        _validate_limit(limit)
        where = "where tenant_id = %s" if tenant_id is not None else ""
        params: tuple[Any, ...] = (tenant_id, limit) if tenant_id is not None else (limit,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select payload
                from shelfwise_worldgen_runs
                {where}
                order by created_at desc, run_id
                limit %s
                """,
                params,
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_worldgen_runs")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_WORLDGEN_RUN_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_worldgen_runs",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def create_worldgen_run_store() -> InMemoryWorldgenRunStore | PostgresWorldgenRunStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryWorldgenRunStore()
    if backend == "postgres":
        return PostgresWorldgenRunStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _run_id(run: dict[str, Any]) -> str:
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("worldgen run must include run_id")
    return run_id


_WORLDGEN_RUN_SCHEMA_SQL = """
create table if not exists shelfwise_worldgen_runs (
    run_id text primary key,
    tenant_id text not null,
    scenario_id text not null,
    seed integer not null,
    status text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null
);
create index if not exists idx_shelfwise_worldgen_runs_tenant_created
on shelfwise_worldgen_runs (tenant_id, created_at desc);
"""
