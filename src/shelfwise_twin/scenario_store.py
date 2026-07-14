"""Durable metadata store for tenant-scoped digital-twin scenario branches."""

from __future__ import annotations

import os
from copy import deepcopy
from threading import RLock
from typing import Any, Protocol

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


class ScenarioBranchStore(Protocol):
    """Persistence contract used by the scenario engine."""

    def create(self, branch: dict[str, Any]) -> dict[str, Any]: ...

    def update(self, branch: dict[str, Any]) -> dict[str, Any]: ...

    def get(
        self, tenant_id: str, store_id: str, branch_id: str
    ) -> dict[str, Any] | None: ...

    def clear(self) -> None: ...


class InMemoryScenarioBranchStore:
    """Thread-safe disposable branch metadata store."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._branches: dict[tuple[str, str, str], dict[str, Any]] = {}

    def create(self, branch: dict[str, Any]) -> dict[str, Any]:
        key = _key(branch)
        with self._lock:
            if key in self._branches:
                raise ValueError("scenario branch already exists")
            self._branches[key] = deepcopy(branch)
        return deepcopy(branch)

    def update(self, branch: dict[str, Any]) -> dict[str, Any]:
        key = _key(branch)
        with self._lock:
            if key not in self._branches:
                raise KeyError("scenario branch not found")
            self._branches[key] = deepcopy(branch)
        return deepcopy(branch)

    def get(
        self, tenant_id: str, store_id: str, branch_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            branch = self._branches.get((tenant_id, store_id, branch_id))
        return deepcopy(branch) if branch is not None else None

    def clear(self) -> None:
        with self._lock:
            self._branches.clear()


class PostgresScenarioBranchStore:
    """Postgres branch metadata store with tenant RLS."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresScenarioBranchStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def create(self, branch: dict[str, Any]) -> dict[str, Any]:
        tenant_id, store_id, branch_id = _key(branch)
        with self._connect(tenant_id) as conn:
            try:
                conn.execute(
                    """
                    insert into shelfwise_twin_scenario_branches
                        (tenant_id, store_id, branch_id, payload, created_at, updated_at)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        store_id,
                        branch_id,
                        jsonb(branch),
                        branch["created_at"],
                        branch["created_at"],
                    ),
                )
                conn.commit()
            except Exception as exc:
                if self.get(tenant_id, store_id, branch_id) is not None:
                    raise ValueError("scenario branch already exists") from exc
                raise
        return deepcopy(branch)

    def update(self, branch: dict[str, Any]) -> dict[str, Any]:
        tenant_id, store_id, branch_id = _key(branch)
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                update shelfwise_twin_scenario_branches
                set payload = %s, updated_at = now()
                where tenant_id = %s and store_id = %s and branch_id = %s
                returning branch_id
                """,
                (jsonb(branch), tenant_id, store_id, branch_id),
            ).fetchone()
            if row is None:
                raise KeyError("scenario branch not found")
            conn.commit()
        return deepcopy(branch)

    def get(
        self, tenant_id: str, store_id: str, branch_id: str
    ) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select payload from shelfwise_twin_scenario_branches
                where tenant_id = %s and store_id = %s and branch_id = %s
                """,
                (tenant_id, store_id, branch_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_twin_scenario_branches")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_twin_scenario_branches",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_scenario_branch_store() -> ScenarioBranchStore:
    """Create the branch store matching the configured application persistence backend."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryScenarioBranchStore()
    if backend == "postgres":
        return PostgresScenarioBranchStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _key(branch: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(branch["tenant_id"]),
        str(branch["store_id"]),
        str(branch["branch_id"]),
    )


_SCHEMA_SQL = """
create table if not exists shelfwise_twin_scenario_branches (
    tenant_id text not null,
    store_id text not null,
    branch_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, store_id, branch_id)
);
create index if not exists idx_shelfwise_twin_scenarios_tenant_store
on shelfwise_twin_scenario_branches (tenant_id, store_id, updated_at desc);
"""
