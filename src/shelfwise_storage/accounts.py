"""Tenant-scoped workforce accounts and authentication lifecycle state."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from .postgres import auto_schema_enabled, connect, jsonb
from .rls import apply_tenant_rls
from .time_utils import now_iso

_ACCOUNT_SCHEMA_SQL = """
create table if not exists shelfwise_work_accounts (
    tenant_id text not null,
    email text not null,
    payload jsonb not null,
    primary key (tenant_id, email)
);
create table if not exists shelfwise_account_audit (
    tenant_id text not null,
    event_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, event_id)
);
"""

_PRIVATE_ACCOUNT_FIELDS = {
    "password_hash",
    "invitation_token_hash",
    "reset_token_hash",
    "session_version",
}


class InMemoryAccountStore:
    """Disposable development/test account store with tenant-scoped lookups."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []

    def create(self, account: dict[str, Any]) -> dict[str, Any]:
        record = _record(account)
        key = (record["tenant_id"], record["email"])
        with self._lock:
            if key in self._accounts:
                raise ValueError("An account with this work email already exists")
            self._accounts[key] = record
        return public_account(record)

    def create_first_owner(self, account: dict[str, Any]) -> dict[str, Any]:
        """Create exactly one bootstrap owner, even under concurrent requests."""
        record = _record({**account, "role": "owner", "active": True, "status": "active"})
        tenant_id = record["tenant_id"]
        with self._lock:
            if any(item["tenant_id"] == tenant_id for item in self._accounts.values()):
                raise ValueError("This client already has a workforce account")
            self._accounts[(tenant_id, record["email"])] = record
        return public_account(record)

    def get_by_email(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._accounts.get((_tenant(tenant_id), _email(email)))
            return deepcopy(item) if item else None

    def get_by_id(self, tenant_id: str, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = next(
                (
                    account
                    for account in self._accounts.values()
                    if account["tenant_id"] == _tenant(tenant_id)
                    and account["id"] == account_id
                ),
                None,
            )
            return deepcopy(item) if item else None

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        tenant = _tenant(tenant_id)
        with self._lock:
            rows = [
                public_account(item)
                for item in self._accounts.values()
                if item["tenant_id"] == tenant
            ]
        return sorted(rows, key=lambda item: (item["surname"], item["given_name"]))

    def set_active(self, tenant_id: str, account_id: str, *, active: bool) -> dict[str, Any] | None:
        status = "active" if active else "inactive"
        return self._update(tenant_id, account_id, {"active": active, "status": status})

    def set_role(self, tenant_id: str, account_id: str, *, role: str) -> dict[str, Any] | None:
        return self._update(tenant_id, account_id, {"role": role})

    def set_password(
        self,
        tenant_id: str,
        account_id: str,
        *,
        password_hash: str,
    ) -> dict[str, Any] | None:
        return self._update(
            tenant_id,
            account_id,
            {"password_hash": password_hash, "must_change_password": False},
        )

    def set_invitation(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        return self._update(
            tenant_id,
            account_id,
            {
                "active": False,
                "status": "invited",
                "invitation_token_hash": token_hash,
                "invitation_expires_at": expires_at,
            },
            invalidate_session=False,
        )

    def activate_invitation(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        password_hash: str,
    ) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        with self._lock:
            for key, account in self._accounts.items():
                if account["tenant_id"] != tenant or account["id"] != account_id:
                    continue
                if (
                    account.get("status") != "invited"
                    or account.get("invitation_token_hash") != token_hash
                    or _expired(account.get("invitation_expires_at"))
                ):
                    return None
                updated = {
                    **account,
                    "active": True,
                    "status": "active",
                    "password_hash": password_hash,
                    "must_change_password": False,
                    "invitation_token_hash": None,
                    "invitation_expires_at": None,
                    "session_version": int(account.get("session_version", 1)) + 1,
                    "updated_at": now_iso(),
                }
                self._accounts[key] = updated
                return public_account(updated)
        return None

    def set_reset_token(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        return self._update(
            tenant_id,
            account_id,
            {"reset_token_hash": token_hash, "reset_expires_at": expires_at},
            invalidate_session=False,
        )

    def consume_reset_token(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        password_hash: str,
    ) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        with self._lock:
            for key, account in self._accounts.items():
                if account["tenant_id"] != tenant or account["id"] != account_id:
                    continue
                if (
                    account.get("reset_token_hash") != token_hash
                    or _expired(account.get("reset_expires_at"))
                ):
                    return None
                updated = {
                    **account,
                    "password_hash": password_hash,
                    "reset_token_hash": None,
                    "reset_expires_at": None,
                    "session_version": int(account.get("session_version", 1)) + 1,
                    "updated_at": now_iso(),
                }
                self._accounts[key] = updated
                return public_account(updated)
        return None

    def record_audit(
        self,
        tenant_id: str,
        *,
        action: str,
        account_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        event = _audit_record(
            tenant_id,
            action=action,
            account_id=account_id,
            actor_id=actor_id,
        )
        with self._lock:
            self._audit.append(event)
        return deepcopy(event)

    def list_audit(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        tenant = _tenant(tenant_id)
        with self._lock:
            rows = [item for item in self._audit if item["tenant_id"] == tenant]
        return [deepcopy(item) for item in rows[-limit:]][::-1]

    def _update(
        self,
        tenant_id: str,
        account_id: str,
        values: dict[str, Any],
        *,
        invalidate_session: bool = True,
    ) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        with self._lock:
            for key, account in self._accounts.items():
                if account["tenant_id"] == tenant and account["id"] == account_id:
                    updated = {**account, **values, "updated_at": now_iso()}
                    if invalidate_session:
                        updated["session_version"] = int(account.get("session_version", 1)) + 1
                    self._accounts[key] = updated
                    return public_account(updated)
        return None

    def clear(self) -> None:
        with self._lock:
            self._accounts.clear()
            self._audit.clear()


class PostgresAccountStore:
    """Durable RLS-scoped workforce accounts with private credential payloads."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresAccountStore")
        self._database_url = database_url
        if auto_schema_enabled():
            with self._connect(None) as conn:
                conn.execute(_ACCOUNT_SCHEMA_SQL)
                apply_tenant_rls(
                    conn,
                    ("shelfwise_work_accounts", "shelfwise_account_audit"),
                )
                conn.commit()

    def create(self, account: dict[str, Any]) -> dict[str, Any]:
        from psycopg.errors import UniqueViolation

        record = _record(account)
        try:
            with self._connect(record["tenant_id"]) as conn:
                self._insert(conn, record)
                conn.commit()
        except UniqueViolation as exc:
            raise ValueError("An account with this work email already exists") from exc
        return public_account(record)

    def create_first_owner(self, account: dict[str, Any]) -> dict[str, Any]:
        record = _record({**account, "role": "owner", "active": True, "status": "active"})
        tenant_id = record["tenant_id"]
        with self._connect(tenant_id) as conn:
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (tenant_id,))
            existing = conn.execute(
                "select 1 from shelfwise_work_accounts where tenant_id = %s limit 1",
                (tenant_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError("This client already has a workforce account")
            self._insert(conn, record)
            conn.commit()
        return public_account(record)

    def get_by_email(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        with self._connect(tenant) as conn:
            row = conn.execute(
                "select payload from shelfwise_work_accounts where tenant_id = %s and email = %s",
                (tenant, _email(email)),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def get_by_id(self, tenant_id: str, account_id: str) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        with self._connect(tenant) as conn:
            row = conn.execute(
                "select payload from shelfwise_work_accounts "
                "where tenant_id = %s and payload->>'id' = %s",
                (tenant, account_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        tenant = _tenant(tenant_id)
        with self._connect(tenant) as conn:
            rows = conn.execute(
                "select payload from shelfwise_work_accounts "
                "where tenant_id = %s order by email",
                (tenant,),
            ).fetchall()
        return [public_account(row["payload"]) for row in rows]

    def set_active(self, tenant_id: str, account_id: str, *, active: bool) -> dict[str, Any] | None:
        return self._update_payload(
            tenant_id,
            account_id,
            {"active": active, "status": "active" if active else "inactive"},
        )

    def set_role(self, tenant_id: str, account_id: str, *, role: str) -> dict[str, Any] | None:
        return self._update_payload(tenant_id, account_id, {"role": role})

    def set_password(
        self,
        tenant_id: str,
        account_id: str,
        *,
        password_hash: str,
    ) -> dict[str, Any] | None:
        return self._update_payload(
            tenant_id,
            account_id,
            {"password_hash": password_hash, "must_change_password": False},
        )

    def set_invitation(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        return self._update_payload(
            tenant_id,
            account_id,
            {
                "active": False,
                "status": "invited",
                "invitation_token_hash": token_hash,
                "invitation_expires_at": expires_at,
            },
            invalidate_session=False,
        )

    def activate_invitation(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        password_hash: str,
    ) -> dict[str, Any] | None:
        return self._consume_token(
            tenant_id,
            account_id,
            token_field="invitation_token_hash",
            expiry_field="invitation_expires_at",
            token_hash=token_hash,
            values={
                "active": True,
                "status": "active",
                "password_hash": password_hash,
                "must_change_password": False,
                "invitation_token_hash": None,
                "invitation_expires_at": None,
            },
        )

    def set_reset_token(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        return self._update_payload(
            tenant_id,
            account_id,
            {"reset_token_hash": token_hash, "reset_expires_at": expires_at},
            invalidate_session=False,
        )

    def consume_reset_token(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_hash: str,
        password_hash: str,
    ) -> dict[str, Any] | None:
        return self._consume_token(
            tenant_id,
            account_id,
            token_field="reset_token_hash",
            expiry_field="reset_expires_at",
            token_hash=token_hash,
            values={
                "password_hash": password_hash,
                "reset_token_hash": None,
                "reset_expires_at": None,
            },
        )

    def record_audit(
        self,
        tenant_id: str,
        *,
        action: str,
        account_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        event = _audit_record(
            tenant_id,
            action=action,
            account_id=account_id,
            actor_id=actor_id,
        )
        with self._connect(event["tenant_id"]) as conn:
            conn.execute(
                "insert into shelfwise_account_audit "
                "(tenant_id, event_id, payload, created_at) values (%s, %s, %s, %s)",
                (
                    event["tenant_id"],
                    event["event_id"],
                    jsonb(event),
                    event["created_at"],
                ),
            )
            conn.commit()
        return deepcopy(event)

    def list_audit(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        tenant = _tenant(tenant_id)
        with self._connect(tenant) as conn:
            rows = conn.execute(
                "select payload from shelfwise_account_audit "
                "where tenant_id = %s order by created_at desc limit %s",
                (tenant, max(1, min(limit, 500))),
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def _insert(self, conn: Any, record: dict[str, Any]) -> None:
        conn.execute(
            "insert into shelfwise_work_accounts "
            "(tenant_id, email, payload) values (%s, %s, %s)",
            (record["tenant_id"], record["email"], jsonb(record)),
        )

    def _update_payload(
        self,
        tenant_id: str,
        account_id: str,
        values: dict[str, Any],
        *,
        invalidate_session: bool = True,
    ) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        values_with_timestamp = {**values, "updated_at": now_iso()}
        session_update = (
            " || jsonb_build_object("
            "'session_version', coalesce((payload->>'session_version')::integer, 1) + 1)"
            if invalidate_session
            else ""
        )
        with self._connect(tenant) as conn:
            row = conn.execute(
                "update shelfwise_work_accounts set payload = payload || %s::jsonb"
                f"{session_update} "
                "where tenant_id = %s and payload->>'id' = %s returning payload",
                (jsonb(values_with_timestamp), tenant, account_id),
            ).fetchone()
            conn.commit()
        return public_account(row["payload"]) if row else None

    def _consume_token(
        self,
        tenant_id: str,
        account_id: str,
        *,
        token_field: str,
        expiry_field: str,
        token_hash: str,
        values: dict[str, Any],
    ) -> dict[str, Any] | None:
        tenant = _tenant(tenant_id)
        values_with_timestamp = {**values, "updated_at": now_iso()}
        with self._connect(tenant) as conn:
            row = conn.execute(
                "update shelfwise_work_accounts "
                "set payload = payload || %s::jsonb || jsonb_build_object("
                "'session_version', coalesce((payload->>'session_version')::integer, 1) + 1) "
                "where tenant_id = %s and payload->>'id' = %s "
                f"and payload->>'{token_field}' = %s "
                f"and (payload->>'{expiry_field}')::timestamptz >= now() "
                "returning payload",
                (jsonb(values_with_timestamp), tenant, account_id, token_hash),
            ).fetchone()
            conn.commit()
        return public_account(row["payload"]) if row else None

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_account_audit")
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
    """Remove credentials, token digests, and session internals from API-safe records."""
    return {
        key: value
        for key, value in account.items()
        if key not in _PRIVATE_ACCOUNT_FIELDS
    }


def _record(account: dict[str, Any]) -> dict[str, Any]:
    required = ("tenant_id", "email", "given_name", "surname", "position", "role")
    result = {key: str(account.get(key) or "").strip() for key in required}
    if not all(result.values()):
        raise ValueError("All work-account identity fields are required")
    password_hash = str(account.get("password_hash") or "").strip()
    status = str(account.get("status") or ("active" if password_hash else "invited")).strip()
    if status == "active" and not password_hash:
        raise ValueError("An active work account requires a password credential")
    result.update(
        {
            "email": _email(result["email"]),
            "id": str(account.get("id") or uuid4()),
            "active": bool(account.get("active", status == "active")),
            "status": status,
            "password_hash": password_hash,
            "must_change_password": bool(account.get("must_change_password", False)),
            "session_version": int(account.get("session_version", 1)),
            "invitation_token_hash": account.get("invitation_token_hash"),
            "invitation_expires_at": account.get("invitation_expires_at"),
            "reset_token_hash": account.get("reset_token_hash"),
            "reset_expires_at": account.get("reset_expires_at"),
            "created_at": str(account.get("created_at") or now_iso()),
            "updated_at": now_iso(),
        }
    )
    return result


def _audit_record(
    tenant_id: str,
    *,
    action: str,
    account_id: str,
    actor_id: str,
) -> dict[str, Any]:
    if not action.strip() or not account_id.strip() or not actor_id.strip():
        raise ValueError("Account audit action, account, and actor are required")
    return {
        "tenant_id": _tenant(tenant_id),
        "event_id": f"acct_audit_{uuid4().hex}",
        "action": action.strip(),
        "account_id": account_id.strip(),
        "actor_id": actor_id.strip(),
        "created_at": now_iso(),
    }


def _expired(value: object) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < datetime.now(UTC)


def _tenant(value: str) -> str:
    tenant_id = value.strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id


def _email(value: str) -> str:
    email = value.strip().lower()
    if (
        "@" not in email
        or email.startswith("@")
        or email.endswith("@")
        or len(email) > 200
    ):
        raise ValueError("A valid work email is required")
    return email
