"""Tenant-scoped workforce accounts; credentials never leave this boundary."""
from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any
from uuid import uuid4

from .postgres import auto_schema_enabled, connect, jsonb
from .rls import apply_tenant_rls

_ACCOUNT_SCHEMA_SQL = """
create table if not exists shelfwise_work_accounts (
    tenant_id text not null,
    email text not null,
    payload jsonb not null,
    primary key (tenant_id, email)
);
"""


class InMemoryAccountStore:
    """Disposable development/test account store with tenant-scoped lookups."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}

    def create(self, account: dict[str, Any]) -> dict[str, Any]:
        record = _record(account)
        key = (record["tenant_id"], record["email"])
        with self._lock:
            if key in self._accounts:
                raise ValueError("An account with this work email already exists")
            self._accounts[key] = record
        return public_account(record)

    def get_by_email(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._accounts.get((tenant_id.strip(), _email(email)))
            return deepcopy(item) if item else None

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                public_account(item)
                for item in self._accounts.values()
                if item["tenant_id"] == tenant_id
            ]
        return sorted(rows, key=lambda item: (item["surname"], item["given_name"]))

    def set_active(self, tenant_id: str, account_id: str, *, active: bool) -> dict[str, Any] | None:
        with self._lock:
            for key, account in self._accounts.items():
                if account["tenant_id"] == tenant_id and account["id"] == account_id:
                    updated = {**account, "active": active}
                    self._accounts[key] = updated
                    return public_account(updated)
        return None

    def set_role(self, tenant_id: str, account_id: str, *, role: str) -> dict[str, Any] | None:
        with self._lock:
            for key, account in self._accounts.items():
                if account["tenant_id"] == tenant_id and account["id"] == account_id:
                    updated = {**account, "role": role}
                    self._accounts[key] = updated
                    return public_account(updated)
        return None

    def clear(self) -> None:
        with self._lock:
            self._accounts.clear()


class PostgresAccountStore:
    """Durable RLS-scoped workforce accounts with private credential payloads."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresAccountStore")
        self._database_url = database_url
        if auto_schema_enabled():
            with self._connect(None) as conn:
                conn.execute(_ACCOUNT_SCHEMA_SQL)
                apply_tenant_rls(conn, ("shelfwise_work_accounts",))
                conn.commit()

    def create(self, account: dict[str, Any]) -> dict[str, Any]:
        from psycopg.errors import UniqueViolation

        record = _record(account)
        try:
            with self._connect(record["tenant_id"]) as conn:
                conn.execute(
                    "insert into shelfwise_work_accounts "
                    "(tenant_id, email, payload) values (%s, %s, %s)",
                    (record["tenant_id"], record["email"], jsonb(record)),
                )
                conn.commit()
        except UniqueViolation as exc:
            raise ValueError("An account with this work email already exists") from exc
        return public_account(record)

    def get_by_email(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        tenant = tenant_id.strip()
        with self._connect(tenant) as conn:
            row = conn.execute(
                "select payload from shelfwise_work_accounts where tenant_id = %s and email = %s",
                (tenant, _email(email)),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        tenant = tenant_id.strip()
        with self._connect(tenant) as conn:
            rows = conn.execute(
                "select payload from shelfwise_work_accounts "
                "where tenant_id = %s order by email",
                (tenant,),
            ).fetchall()
        return [public_account(row["payload"]) for row in rows]

    def set_active(self, tenant_id: str, account_id: str, *, active: bool) -> dict[str, Any] | None:
        return self._update_payload(tenant_id, account_id, {"active": active})

    def set_role(self, tenant_id: str, account_id: str, *, role: str) -> dict[str, Any] | None:
        return self._update_payload(tenant_id, account_id, {"role": role})

    def _update_payload(
        self, tenant_id: str, account_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Atomically merge `values` into the account's payload in one round trip.

        A prior version did select-all-then-filter-in-Python-then-update - not just an
        O(n) full-tenant scan for every single role/status change, but a genuine
        check-then-act race: two owners changing the same account concurrently could lose
        one update. `payload || %s::jsonb` merges server-side in the same statement that
        finds the row, so there is no window between "read" and "write" for a concurrent
        change to land in.
        """
        tenant = tenant_id.strip()
        with self._connect(tenant) as conn:
            row = conn.execute(
                "update shelfwise_work_accounts set payload = payload || %s::jsonb "
                "where tenant_id = %s and payload->>'id' = %s "
                "returning payload",
                (jsonb(values), tenant, account_id),
            ).fetchone()
            conn.commit()
        return public_account(row["payload"]) if row else None

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_work_accounts")
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_account_store() -> InMemoryAccountStore | PostgresAccountStore:
    """Select the same explicit memory/Postgres backend as the rest of the platform."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryAccountStore()
    if backend == "postgres":
        return PostgresAccountStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in account.items() if key != "password_hash"}


def _record(account: dict[str, Any]) -> dict[str, Any]:
    required = ("tenant_id", "email", "given_name", "surname", "position", "role", "password_hash")
    result = {key: str(account.get(key) or "").strip() for key in required}
    if not all(result.values()):
        raise ValueError("All work-account fields are required")
    result["email"] = _email(result["email"])
    result["id"] = str(account.get("id") or uuid4())
    result["active"] = bool(account.get("active", True))
    return result


def _email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or len(email) > 200:
        raise ValueError("A valid work email is required")
    return email
