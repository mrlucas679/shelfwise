from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage import now_iso as _now
from shelfwise_storage.rls import apply_tenant_rls


class TaskWriteBackSink:
    """Recommend-only write-back sink that creates idempotent manager tasks."""

    def __init__(self) -> None:
        self._tasks_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    def create_task(
        self,
        *,
        idempotency_key: str,
        title: str,
        action: dict[str, Any],
        tenant_id: str,
        data_domain: str = "operational_twin",
        assignee_role: str = "manager",
        rollback_instructions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = (tenant_id, data_domain, idempotency_key)
        if key in self._tasks_by_key:
            return deepcopy(self._tasks_by_key[key])
        now = _now()
        task = {
            "id": f"task_{len(self._tasks_by_key) + 1}",
            "idempotency_key": idempotency_key,
            "tenant_id": tenant_id,
            "data_domain": data_domain,
            "assignee_role": assignee_role,
            "title": title,
            "action": deepcopy(action),
            "status": "pending_external_write",
            "rollback_instructions": deepcopy(rollback_instructions or {}),
            "created_at": now,
            "updated_at": now,
        }
        self._tasks_by_key[key] = task
        return deepcopy(task)

    def list(
        self, *, tenant_id: str | None = None, data_domain: str | None = None
    ) -> list[dict[str, Any]]:
        tasks = list(self._tasks_by_key.values())
        if tenant_id is not None:
            tasks = [task for task in tasks if task.get("tenant_id") == tenant_id]
        if data_domain is not None:
            tasks = [task for task in tasks if task.get("data_domain") == data_domain]
        return [deepcopy(task) for task in tasks]

    def complete_task(
        self,
        *,
        task_id: str,
        tenant_id: str,
        receipt: dict[str, Any],
        data_domain: str = "operational_twin",
    ) -> dict[str, Any] | None:
        task = next(
            (
                item
                for item in self._tasks_by_key.values()
                if item["id"] == task_id and item["tenant_id"] == tenant_id
                and item["data_domain"] == data_domain
            ),
            None,
        )
        if task is None:
            return None
        if task["status"] == "completed":
            if task.get("completion_receipt") != receipt:
                raise ValueError("task already has a different completion receipt")
            return deepcopy(task)
        now = _now()
        task["status"] = "completed"
        task["completion_receipt"] = deepcopy(receipt)
        task["completed_at"] = now
        task["updated_at"] = now
        return deepcopy(task)

    def clear(self) -> None:
        self._tasks_by_key.clear()


class PostgresTaskWriteBackSink:
    """Postgres-backed task/draft sink for HITL-gated write-back."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresTaskWriteBackSink")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def create_task(
        self,
        *,
        idempotency_key: str,
        title: str,
        action: dict[str, Any],
        tenant_id: str,
        data_domain: str = "operational_twin",
        assignee_role: str = "manager",
        rollback_instructions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self._get(
            tenant_id=tenant_id,
            data_domain=data_domain,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        now = _now()
        task_id = f"task_{_task_hash(tenant_id, data_domain, idempotency_key)}"
        payload = {
            "id": task_id,
            "idempotency_key": idempotency_key,
            "tenant_id": tenant_id,
            "data_domain": data_domain,
            "assignee_role": assignee_role,
            "title": title,
            "action": deepcopy(action),
            "status": "pending_external_write",
            "rollback_instructions": deepcopy(rollback_instructions or {}),
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into shelfwise_writeback_tasks
                    (
                        tenant_id, data_domain, idempotency_key, task_id, title, assignee_role,
                        action, status, rollback_instructions, payload,
                        created_at, updated_at
                    )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, data_domain, idempotency_key) do nothing
                """,
                (
                    tenant_id,
                    data_domain,
                    idempotency_key,
                    task_id,
                    title,
                    assignee_role,
                    jsonb(action),
                    "pending_external_write",
                    jsonb(rollback_instructions or {}),
                    jsonb(payload),
                    now,
                    now,
                ),
            )
            conn.commit()
        return self._get(
            tenant_id=tenant_id,
            data_domain=data_domain,
            idempotency_key=idempotency_key,
        ) or payload

    def list(
        self, *, tenant_id: str | None = None, data_domain: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if data_domain is not None:
            clauses.append("data_domain = %s")
            params.append(data_domain)
        where = "where " + " and ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select payload
                from shelfwise_writeback_tasks
                {where}
                order by created_at desc, task_id asc
                """,
                tuple(params),
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def complete_task(
        self,
        *,
        task_id: str,
        tenant_id: str,
        receipt: dict[str, Any],
        data_domain: str = "operational_twin",
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload from shelfwise_writeback_tasks
                where tenant_id = %s and data_domain = %s and task_id = %s
                for update
                """,
                (tenant_id, data_domain, task_id),
            ).fetchone()
            if row is None:
                return None
            task = deepcopy(row["payload"])
            if task.get("status") == "completed":
                if task.get("completion_receipt") != receipt:
                    raise ValueError("task already has a different completion receipt")
            else:
                now = _now()
                task["status"] = "completed"
                task["completion_receipt"] = deepcopy(receipt)
                task["completed_at"] = now
                task["updated_at"] = now
                conn.execute(
                    """
                    update shelfwise_writeback_tasks
                    set status = 'completed', payload = %s, updated_at = %s
                    where tenant_id = %s and data_domain = %s and task_id = %s
                    """,
                    (jsonb(task), now, tenant_id, data_domain, task_id),
                )
                conn.commit()
            return task

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_writeback_tasks")
            conn.commit()

    def _get(
        self, *, tenant_id: str, data_domain: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload
                from shelfwise_writeback_tasks
                where tenant_id = %s and data_domain = %s and idempotency_key = %s
                """,
                (tenant_id, data_domain, idempotency_key),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_WRITEBACK_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_writeback_tasks",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def create_writeback_sink() -> TaskWriteBackSink | PostgresTaskWriteBackSink:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return TaskWriteBackSink()
    if backend == "postgres":
        return PostgresTaskWriteBackSink(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _task_hash(tenant_id: str, data_domain: str, idempotency_key: str) -> str:
    return hashlib.sha256(
        f"{tenant_id}:{data_domain}:{idempotency_key}".encode()
    ).hexdigest()[:16]


_WRITEBACK_SCHEMA_SQL = """
create table if not exists shelfwise_writeback_tasks (
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    idempotency_key text not null,
    task_id text not null unique,
    title text not null,
    assignee_role text not null,
    action jsonb not null,
    status text not null,
    rollback_instructions jsonb not null default '{}',
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, data_domain, idempotency_key)
);
alter table shelfwise_writeback_tasks
add column if not exists data_domain text not null default 'operational_twin';
alter table shelfwise_writeback_tasks
drop constraint if exists shelfwise_writeback_tasks_pkey;
alter table shelfwise_writeback_tasks
add primary key (tenant_id, data_domain, idempotency_key);
drop index if exists idx_shelfwise_writeback_tasks_tenant_created;
create index if not exists idx_shelfwise_writeback_tasks_tenant_created
on shelfwise_writeback_tasks (tenant_id, data_domain, created_at desc);
"""
