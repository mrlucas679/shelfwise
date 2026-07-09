from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


@dataclass(frozen=True, slots=True)
class JournalRun:
    run_id: str
    tenant_id: str
    status: str
    started_at: str
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class InMemoryJournal:
    def __init__(self) -> None:
        self._lock = Lock()
        self._runs: dict[str, JournalRun] = {}
        self._steps: dict[tuple[str, str], dict[str, Any]] = {}
        self._compensations: dict[str, list[dict[str, Any]]] = {}

    def start_run(self, run_id: str, *, tenant_id: str) -> None:
        with self._lock:
            self._runs.setdefault(
                run_id,
                JournalRun(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    status="running",
                    started_at=_now(),
                ),
            )

    def finish_run(self, run_id: str, *, status: str) -> None:
        with self._lock:
            existing = self._runs.get(run_id)
            tenant_id = existing.tenant_id if existing else "default"
            started_at = existing.started_at if existing else _now()
            self._runs[run_id] = JournalRun(
                run_id=run_id,
                tenant_id=tenant_id,
                status=status,
                started_at=started_at,
                finished_at=_now(),
            )

    def get(self, run_id: str, step_key: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._steps.get((run_id, step_key))
            return deepcopy(found) if found is not None else None

    def put(
        self,
        run_id: str,
        step_key: str,
        output: dict[str, Any],
        *,
        compensation: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._steps.setdefault((run_id, step_key), deepcopy(output))
            if compensation is not None:
                self._compensations.setdefault(run_id, []).append(deepcopy(compensation))

    def compensations(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item) for item in reversed(self._compensations.get(run_id, []))]

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._runs.values(), key=lambda item: item.started_at)
            return [run.to_dict() for run in ordered]

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._steps.clear()
            self._compensations.clear()


class PostgresJournal:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresJournal")
        self._database_url = database_url
        self._ensure_schema()

    def start_run(self, run_id: str, *, tenant_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into cascade_runs (run_id, tenant_id, status, started_at)
                values (%s, %s, 'running', %s)
                on conflict (run_id) do nothing
                """,
                (run_id, tenant_id, _now()),
            )
            conn.commit()

    def finish_run(self, run_id: str, *, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update cascade_runs
                set status = %s,
                    finished_at = %s
                where run_id = %s
                """,
                (status, _now(), run_id),
            )
            conn.commit()

    def get(self, run_id: str, step_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select output
                from cascade_steps
                where run_id = %s and step_key = %s
                """,
                (run_id, step_key),
            ).fetchone()
        return deepcopy(row["output"]) if row else None

    def put(
        self,
        run_id: str,
        step_key: str,
        output: dict[str, Any],
        *,
        compensation: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            run = conn.execute(
                "select tenant_id from cascade_runs where run_id = %s",
                (run_id,),
            ).fetchone()
            tenant_id = run["tenant_id"] if run else "default"
            conn.execute(
                """
                insert into cascade_steps
                    (run_id, tenant_id, step_key, output, compensation, recorded_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (run_id, step_key) do nothing
                """,
                (
                    run_id,
                    tenant_id,
                    step_key,
                    jsonb(output),
                    jsonb(compensation) if compensation is not None else None,
                    _now(),
                ),
            )
            conn.commit()

    def compensations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select compensation
                from cascade_steps
                where run_id = %s and compensation is not null
                order by recorded_at desc
                """,
                (run_id,),
            ).fetchall()
        return [deepcopy(row["compensation"]) for row in rows]

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select run_id, tenant_id, status, started_at, finished_at
                from cascade_runs
                order by started_at desc, run_id
                limit 200
                """
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "tenant_id": row["tenant_id"],
                "status": row["status"],
                "started_at": row["started_at"].isoformat(),
                "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            }
            for row in rows
        ]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from cascade_steps")
            conn.execute("delete from cascade_runs")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists cascade_runs (
                    run_id text primary key,
                    tenant_id text not null,
                    status text not null default 'running',
                    started_at timestamptz not null,
                    finished_at timestamptz
                )
                """
            )
            conn.execute(
                """
                create table if not exists cascade_steps (
                    run_id text not null references cascade_runs(run_id) on delete cascade,
                    tenant_id text not null default 'default',
                    step_key text not null,
                    output jsonb not null,
                    compensation jsonb,
                    recorded_at timestamptz not null,
                    primary key (run_id, step_key)
                )
                """
            )
            conn.execute(
                """
                alter table cascade_steps
                add column if not exists tenant_id text not null default 'default'
                """
            )
            conn.execute(
                """
                create index if not exists idx_cascade_steps_tenant_run
                on cascade_steps (tenant_id, run_id)
                """
            )
            apply_tenant_rls(conn, ("cascade_runs", "cascade_steps"))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def journaled(
    journal: InMemoryJournal | PostgresJournal,
    run_id: str,
    step_key: str,
    fn: Callable[[], dict[str, Any]],
    *,
    compensation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seen = journal.get(run_id, step_key)
    if seen is not None:
        return seen
    output = fn()
    journal.put(run_id, step_key, output, compensation=compensation)
    return deepcopy(output)


def create_journal() -> InMemoryJournal | PostgresJournal:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryJournal()
    if backend == "postgres":
        return PostgresJournal(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
