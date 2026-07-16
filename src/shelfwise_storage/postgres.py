from __future__ import annotations

import os
import sys
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


def auto_schema_enabled() -> bool:
    """Allow local stores to self-bootstrap; production migrations disable this."""
    return os.getenv("SHELFWISE_AUTO_SCHEMA", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


def _pooling_enabled() -> bool:
    """Pooled connections are the production default; SHELFWISE_DB_POOL=false restores
    one-connection-per-call for debugging a suspected pool interaction."""
    return os.getenv("SHELFWISE_DB_POOL", "true").strip().lower() in {"1", "true", "yes", "on"}


def _pool_bound(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _configure_pooled_connection(conn: Any) -> None:
    """Run once per physical connection the pool creates: row shape + RLS-role check."""
    from psycopg.rows import dict_row

    conn.row_factory = dict_row
    if not _allow_superuser_db():
        row = conn.execute(
            "select rolname, rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        _check_role_is_not_superuser(row)
    conn.commit()


def _reset_pooled_connection(conn: Any) -> None:
    """Run at check-in: never let one caller's tenant leak into the next checkout.

    Every checkout re-binds app.tenant_id before the connection is handed out, so this
    clear is defense in depth, not the primary isolation mechanism.
    """
    conn.execute("select set_config('app.tenant_id', '', false)")
    conn.commit()


def _pool_for(database_url: str) -> Any:
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError("Install psycopg[pool] to use pooled Postgres storage") from exc
    pool = _pools.get(database_url)
    if pool is not None:
        return pool
    with _pools_lock:
        pool = _pools.get(database_url)
        if pool is None:
            pool = ConnectionPool(
                database_url,
                min_size=_pool_bound("SHELFWISE_DB_POOL_MIN", 1),
                max_size=_pool_bound("SHELFWISE_DB_POOL_MAX", 10),
                configure=_configure_pooled_connection,
                reset=_reset_pooled_connection,
                open=True,
            )
            _pools[database_url] = pool
        return pool


class _TenantBoundPooledConnection:
    """Checkout wrapper preserving the `with connect(...) as conn` store contract.

    The pool's own context manager commits on clean exit, rolls back on exception, and
    returns the connection to the pool instead of closing it. The tenant is bound
    session-level at every checkout, which (unlike the transaction-local binding the
    unpooled path used historically) also survives an intermediate commit inside one
    store method instead of silently reverting RLS to no-tenant mid-scope.
    """

    def __init__(self, database_url: str, tenant_id: str) -> None:
        self._cm = _pool_for(database_url).connection()
        self._tenant_id = tenant_id

    def __enter__(self) -> Any:
        conn = self._cm.__enter__()
        try:
            conn.execute(
                "select set_config('app.tenant_id', %s, false)", (self._tenant_id,)
            )
        except BaseException:
            self._cm.__exit__(*sys.exc_info())
            raise
        return conn

    def __exit__(self, *exc: Any) -> Any:
        return self._cm.__exit__(*exc)


def connect(database_url: str, *, tenant_id: str | None = None) -> Any:
    resolved_tenant = _clean_tenant_id(tenant_id) if tenant_id else current_tenant_id()
    if _pooling_enabled():
        return _TenantBoundPooledConnection(database_url, resolved_tenant)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to use Postgres storage") from exc
    conn = psycopg.connect(database_url, row_factory=dict_row)
    _verify_role_is_not_superuser_once(conn)
    conn.execute(
        "select set_config('app.tenant_id', %s, true)",
        (resolved_tenant,),
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
        os.getenv("SHELFWISE_TENANT_ID") or os.getenv("TENANT_ID") or "local"
    )


def _clean_tenant_id(value: str | None) -> str:
    tenant_id = str(value or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id
