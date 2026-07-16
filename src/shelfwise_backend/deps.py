"""Auth, tenant-resolution, and rate-limit dependency wiring for the ShelfWise API.

Every FastAPI dependency that decides "who is this request from, and are they allowed to
do this" lives here: auth-mode/tenant resolution, role-gated dependencies, the write-path
API-key guard, the worker-internal credential guard, and the shared write-rate limiter.
Route modules (in `app.py` today, in per-domain routers as they're extracted) import these
instead of each redefining tenant/role checks.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, Header, HTTPException, Request

from .security.gateway import TokenBucket, rate_limit
from .tenant import Role, TenantContext, default_tenant_context, verify_bearer_token

_INSECURE_APP_ENV_NAMES = {"production", "prod", "staging", "stage"}
_PRODUCTION_APP_ENV_NAMES = _INSECURE_APP_ENV_NAMES
_COOKIE_OVERRIDE_ENV = "SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}

# Sentinel tenant id bound for the RLS session variable when a request in jwt mode carries
# no valid token. It matches no real tenant row, so a request that skips the route-level
# `current_tenant_context`/`require_role` dependency still can't read another tenant's data
# under RLS - a bad/missing token must never fall back to a real (demo) tenant's context.
_UNAUTHENTICATED_TENANT_ID = "__unauthenticated__"

SESSION_COOKIE = "shelfwise_session"


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _is_production_deployment() -> bool:
    """Identify named deployments where live AMD inference is mandatory."""
    return os.getenv("APP_ENV", "local").strip().lower() in _PRODUCTION_APP_ENV_NAMES


def _cookie_secure_setting() -> bool:
    """Return the cookie Secure setting and reject ambiguous environment values."""
    raw = os.getenv("SHELFWISE_COOKIE_SECURE", "true").strip().lower()
    if raw in _TRUE_ENV_VALUES:
        return True
    if raw in _FALSE_ENV_VALUES:
        return False
    raise RuntimeError("SHELFWISE_COOKIE_SECURE must be a boolean value: true or false")


def _auth_mode() -> str:
    return os.getenv("SHELFWISE_AUTH_MODE", "off").strip().lower()


def _request_authorization(request: Request, authorization: str | None = None) -> str | None:
    if authorization:
        return authorization
    header = request.headers.get("authorization")
    if header:
        return header
    token = request.cookies.get(SESSION_COOKIE)
    return f"Bearer {token}" if token else None


def _tenant_id_from_request(request: Request) -> str:
    if _auth_mode() != "jwt":
        return default_tenant_context().tenant_id
    try:
        return verify_bearer_token(
            _request_authorization(request),
            secret=os.getenv("TENANT_AUTH_SECRET", ""),
        ).tenant_id
    except ValueError:
        return _UNAUTHENTICATED_TENANT_ID


def write_path_guard(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    expected = os.getenv("API_KEY", "")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def worker_internal_guard(
    x_worker_key: str | None = Header(default=None, alias="x-worker-key"),
) -> None:
    """Keep global queue consumption behind a service credential in named deployments."""
    if not _is_production_deployment():
        return
    expected = os.getenv("SHELFWISE_WORKER_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker control plane is unavailable")
    if not x_worker_key or not hmac.compare_digest(x_worker_key, expected):
        raise HTTPException(status_code=401, detail="Invalid worker credential")


def current_tenant_context(
    request: Request,
    authorization: str | None = Header(default=None, alias="authorization"),
) -> TenantContext:
    mode = _auth_mode()
    if mode == "off":
        return default_tenant_context()
    if mode != "jwt":
        raise HTTPException(status_code=500, detail="Unsupported auth mode")
    try:
        return verify_bearer_token(
            _request_authorization(request, authorization),
            secret=os.getenv("TENANT_AUTH_SECRET", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid tenant token") from exc


CURRENT_TENANT_DEP = Depends(current_tenant_context)


def require_role(*allowed: Role):
    allowed_set = set(allowed)

    def dependency(ctx: TenantContext = CURRENT_TENANT_DEP) -> TenantContext:
        if ctx.role not in allowed_set:
            raise HTTPException(status_code=403, detail="Role is not allowed for this action")
        return ctx

    return dependency


INGEST_AUTH = require_role(Role.OWNER, Role.MANAGER, Role.INVENTORY)
APPROVAL_AUTH = require_role(Role.OWNER, Role.EXECUTIVE, Role.MANAGER)
WORKER_AUTH = require_role(Role.OWNER, Role.MANAGER)
OWNER_AUTH = require_role(Role.OWNER)
INGEST_AUTH_DEP = Depends(INGEST_AUTH)
APPROVAL_AUTH_DEP = Depends(APPROVAL_AUTH)
WORKER_AUTH_DEP = Depends(WORKER_AUTH)
OWNER_AUTH_DEP = Depends(OWNER_AUTH)

# Operator knob: unattended harness/soak runs legitimately push write rates far past
# interactive-use defaults. Defaults stay identical when the env vars are unset.
write_limiter = TokenBucket(
    capacity=_env_positive_int("SHELFWISE_WRITE_RATE_CAPACITY", 240),
    refill_per_s=_env_positive_float("SHELFWISE_WRITE_RATE_REFILL_PER_S", 8.0),
)
WRITE_LIMIT_DEP = Depends(rate_limit(write_limiter))
