from __future__ import annotations

import os
import threading
from contextvars import ContextVar, Token
from typing import Any

_TENANT_ID: ContextVar[str | None] = ContextVar("shelfwise_tenant_id", default=None)
_role_verified = False
_role_verify_lock = threading.Lock()


def bind_tenant_context(tenant_id: str) -> Token[str | None]:
    return _TENANT_ID.set(_clean_tenant_id(tenant_id))


def reset_tenant_context(token: Token[str | None]) -> None:
    _TENANT_ID.reset(token)


def current_tenant_id() -> str:
    tenant_id = _TENANT_ID.get()
    if tenant_id:
        return tenant_id
    return _default_tenant_id()


def connect(database_url: str, *, tenant_id: str | None = None) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to use Postgres storage") from exc
    conn = psycopg.connect(database_url, row_factory=dict_row)
    _verify_role_is_not_superuser_once(conn)
    conn.execute(
        "select set_config('app.tenant_id', %s, true)",
        (_clean_tenant_id(tenant_id) if tenant_id else current_tenant_id(),),
    )
    return conn


def _check_role_is_not_superuser(row: dict[str, Any] | None) -> None:
    """Raise if the connected role is a superuser or has BYPASSRLS.

    Postgres does not enforce row-level security (even FORCE ROW LEVEL SECURITY) for
    superusers or BYPASSRLS roles, so every tenant-isolation policy `apply_tenant_rls`
    installs would be silently inert if the app ever connects as one. Fail closed instead.
    """
    if row and (row.get("rolsuper") or row.get("rolbypassrls")):
        raise RuntimeError(
            f"Postgres role '{row.get('rolname')}' is a superuser or has BYPASSRLS - "
            "tenant row-level-security policies would be silently bypassed. Connect as "
            "a least-privilege application role (NOSUPERUSER NOBYPASSRLS) instead; see "
            "the shelfwise_app role created in schema.sql. Set "
            "SHELFWISE_ALLOW_SUPERUSER_DB=true to override for local debugging only."
        )


def _allow_superuser_db() -> bool:
    return os.getenv("SHELFWISE_ALLOW_SUPERUSER_DB", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _verify_role_is_not_superuser_once(conn: Any) -> None:
    """Check the connected role once per process, not on every connection (cheap check,
    but there is no true single startup hook here yet - connect() is called per request)."""
    global _role_verified
    if _role_verified:
        return
    if _allow_superuser_db():
        _role_verified = True
        return
    with _role_verify_lock:
        if _role_verified:
            return
        row = conn.execute(
            "select rolname, rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        _check_role_is_not_superuser(row)
        _role_verified = True


def jsonb(value: dict[str, Any]) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to use Postgres storage") from exc
    return Jsonb(value)


def _default_tenant_id() -> str:
    return _clean_tenant_id(
        os.getenv("SHELFWISE_TENANT_ID") or os.getenv("TENANT_ID") or "sa_retail_demo"
    )


def _clean_tenant_id(value: str | None) -> str:
    tenant_id = str(value or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id
