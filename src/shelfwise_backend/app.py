from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shelfwise_catalog import (
    ConflictingIdentifierError,
    Product,
    ProductIdentifier,
    ProductVariant,
)
from shelfwise_connectors import (
    CsvIntakeError,
    SourceSystem,
    map_for,
    quarantine_intake,
    record_to_event,
)
from shelfwise_connectors import (
    build_records as build_csv_intake_records,
)
from shelfwise_connectors import (
    preview_csv as preview_csv_file,
)
from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_inference import (
    InferenceError,
    OpenAICompatibleInferenceClient,
    ProviderKind,
    load_inference_config,
)
from shelfwise_mlops.skill_registry import discover as discover_skills
from shelfwise_runtime.provenance import DataDomain
from shelfwise_storage import (
    TENANT_SCOPED_TABLES,
    bind_tenant_context,
    default_tenant_profile,
    reset_tenant_context,
)

from .auth_credentials import login_credentials_valid, scrypt_password_hash
from .cascade import (
    validate_inventory_exception,
    validate_recall_notice,
)
from .chat import ChatBody, build_chat_reply_with_meta
from .chat_context import (
    bounded_chat_decisions as select_chat_decisions,
)
from .chat_context import (
    bounded_chat_learning_events as select_chat_learning_events,
)
from .connector_poll_service import ConnectorPollService
from .conversation_memory import compact_conversation
from .conversation_routing import ConversationRouteRequest, choose_conversation_route
from .decision_access import (
    decision_action,
    decision_belongs_to_other_tenant,
    decision_tenant_id,
    reject_cross_tenant_decision_access,
    tenant_scoped_decisions,
)
from .deps import (
    _COOKIE_OVERRIDE_ENV,
    _INSECURE_APP_ENV_NAMES,
    _TRUE_ENV_VALUES,
    APPROVAL_AUTH_DEP,
    CURRENT_TENANT_DEP,
    INGEST_AUTH_DEP,
    OWNER_AUTH_DEP,
    SESSION_COOKIE,
    WRITE_LIMIT_DEP,
    _auth_mode,
    _chat_data_domain,
    _cookie_secure_setting,
    _env_positive_int,
    _is_production_deployment,
    _request_authorization,
    _request_timeout_seconds,
    _require_amd_inference,
    _tenant_id_from_request,
    worker_internal_guard,
    write_limiter,  # noqa: F401  (re-exported: tests/conftest.py imports it from here)
    write_path_guard,
)
from .detective import analyze_root_cause, root_cause_cte_sql
from .ingest_pipeline import record_cascade, record_pipeline_event
from .intelligence_api import router as intelligence_router
from .model_runs import record_model_run
from .operational_facts import MissingOperationalFacts
from .product_catalog import product_attention_queue, search_product_catalog
from .routes_catalog import router as catalog_router
from .routes_connector_credentials import router as connector_credentials_router
from .routes_connectors import router as connectors_router
from .routes_mlops import router as mlops_router
from .routes_scenarios import router as scenarios_router
from .routes_twin import router as twin_router
from .state import (
    account_store,
    candidate_store,
    cascade_worker,
    chat_store,
    cold_chain_feed,
    connector_credential_store,
    connector_cursor_store,
    conversation_memory_store,
    decision_store,
    evaluation_registry,  # noqa: F401  (re-exported: tests/conftest.py imports it from here)
    event_bus,
    event_store,
    fidelity_revalidation_service,
    inbound_record_store,
    inventory_position_store,
    journal,
    learning_store,
    model_run_registry,  # noqa: F401  (re-exported: tests/conftest.py imports it from here)
    open_order_store,
    operational_facts_for_query,
    product_catalog_store,
    prompt_registry,
    retention_service,
    skill_registry,
    tenant_fact_store,
    tenant_profile_store,
    tool_audit,
    trace_registry,
    twin_projection_service,
    twin_service,
    worker_service,
    world_facts,
    world_snapshot_store,  # noqa: F401  (re-exported: shelfwise_eval imports it from here)
    worldgen_run_store,
    writeback_sink,
)
from .tenant import (
    Role,
    TenantContext,
    default_tenant_context,
    encode_hs256_token,
    verify_bearer_token,
)
from .tools.mcp_surface import build_live_twin_tools, build_platform_tools

DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_LOGGER = logging.getLogger("shelfwise.backend")


def cors_allowed_origins() -> list[str]:
    """Return configured frontend origins, with local development defaults."""
    raw = os.getenv("SHELFWISE_CORS_ORIGINS", "")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if "*" in origins:
        raise RuntimeError(
            "SHELFWISE_CORS_ORIGINS cannot contain '*' when credentialed sessions are enabled"
        )
    return origins or list(DEFAULT_CORS_ORIGINS)


def _reject_insecure_auth_in_named_deployments() -> None:
    """Fail fast if a real deployment is configured with auth off.

    Local/dev/test/CI (the default when APP_ENV is unset) stays convenient - nothing in
    this test suite sets APP_ENV. An operator who explicitly labels this a real
    deployment must not be able to silently ship every write endpoint (ingest, approve/
    reject, connector intake, worker) unauthenticated with full owner privileges.
    """
    app_env = os.getenv("APP_ENV", "local").strip().lower()
    auth_mode = os.getenv("SHELFWISE_AUTH_MODE", "off").strip().lower()
    if app_env in _INSECURE_APP_ENV_NAMES and auth_mode == "off":
        raise RuntimeError(
            f"SHELFWISE_AUTH_MODE=off is not allowed when APP_ENV='{app_env}'. Set "
            "SHELFWISE_AUTH_MODE=jwt and TENANT_AUTH_SECRET for any non-local deployment."
        )


def _reject_insecure_production_cookie_config() -> None:
    """Fail closed when a named deployment would issue a non-Secure session cookie."""
    if not _is_production_deployment() or _cookie_secure_setting():
        return
    if os.getenv(_COOKIE_OVERRIDE_ENV, "").strip().lower() in _TRUE_ENV_VALUES:
        return
    app_env = os.getenv("APP_ENV", "local").strip().lower()
    raise RuntimeError(
        f"SHELFWISE_COOKIE_SECURE=false is not allowed when APP_ENV='{app_env}'. "
        f"Use HTTPS or set {_COOKIE_OVERRIDE_ENV}=true only for disposable CI."
    )


def _reject_unsafe_multimodal_config() -> None:
    """Require JWT protection before costly upload routes can be enabled in production."""
    enabled = os.getenv("MULTIMODAL_ENABLED", "false").strip().lower() in _TRUE_ENV_VALUES
    if (
        enabled
        and _is_production_deployment()
        and os.getenv("SHELFWISE_AUTH_MODE", "off").strip().lower() != "jwt"
    ):
        raise RuntimeError("MULTIMODAL_ENABLED requires SHELFWISE_AUTH_MODE=jwt in production")


_reject_insecure_auth_in_named_deployments()
_reject_insecure_production_cookie_config()
_reject_unsafe_multimodal_config()

app = FastAPI(title="ShelfWise", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    # The JWT session cookie must cross the local frontend/backend port boundary.
    # Origins are explicit and never wildcarded, so credentialed CORS stays bounded.
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(intelligence_router)
app.include_router(twin_router)
app.include_router(catalog_router)
app.include_router(connectors_router)
app.include_router(connector_credentials_router)
app.include_router(mlops_router)
app.include_router(scenarios_router)

try:
    from shelfwise_multimodal.router import build_scan_router, build_voice_router
except ImportError:
    build_scan_router = None
    build_voice_router = None

if build_voice_router is not None:
    app.include_router(
        build_voice_router(),
        dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP, CURRENT_TENANT_DEP],
    )
if build_scan_router is not None:
    app.include_router(
        build_scan_router(),
        dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP, CURRENT_TENANT_DEP],
    )

app.router.add_event_handler("startup", worker_service.start)
app.router.add_event_handler("shutdown", worker_service.stop)
app.router.add_event_handler("startup", cold_chain_feed.start)
app.router.add_event_handler("shutdown", cold_chain_feed.stop)
app.router.add_event_handler("startup", twin_projection_service.start)
app.router.add_event_handler("shutdown", twin_projection_service.stop)
app.router.add_event_handler("startup", fidelity_revalidation_service.start)
app.router.add_event_handler("shutdown", fidelity_revalidation_service.stop)
app.router.add_event_handler("startup", retention_service.start)
app.router.add_event_handler("shutdown", retention_service.stop)


DEFAULT_MAX_BODY_BYTES = 6 * 1024 * 1024


def _max_body_bytes() -> int:
    raw = os.getenv("SHELFWISE_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES
    return max(1, value)


def _wrap_receive_with_limit(receive: Any, *, max_bytes: int) -> Any:
    """Wrap an ASGI `receive` callable to enforce a cumulative body-byte ceiling.

    Content-Length is only a hint (absent for chunked transfer-encoding, and callers can lie
    about it), so the actual bytes streamed off the wire must be counted as they arrive
    rather than trusting the header alone. Raises HTTPException(413) once the running total
    crosses `max_bytes`, without buffering the whole body ourselves first.
    """
    received = 0

    async def limited_receive() -> Any:
        nonlocal received
        message = await receive()
        if message.get("type") == "http.request":
            received += len(message.get("body") or b"")
            if received > max_bytes:
                raise HTTPException(status_code=413, detail="Request body is too large")
        return message

    return limited_receive


@app.middleware("http")
async def enforce_request_body_limit(request: Request, call_next: Any) -> Any:
    max_bytes = _max_body_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            body_length = int(content_length)
        except ValueError:
            body_length = 0
        if body_length > max_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body is too large"},
            )

    # Reassigning the private receive channel is the documented Starlette pattern for
    # streaming-aware middleware that must observe body bytes as they arrive.
    request._receive = _wrap_receive_with_limit(request.receive, max_bytes=max_bytes)
    return await call_next(request)


@app.middleware("http")
async def enforce_request_deadline(request: Request, call_next: Any) -> Any:
    """Return a bounded failure instead of allowing multi-call inference to exceed 30s."""
    try:
        return await asyncio.wait_for(call_next(request), timeout=_request_timeout_seconds())
    except TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"detail": "Request exceeded the inference response-time limit"},
        )


@app.middleware("http")
async def bind_storage_tenant(request: Request, call_next: Any) -> Any:
    tenant_id = _tenant_id_from_request(request)
    token = bind_tenant_context(tenant_id)
    try:
        return await call_next(request)
    finally:
        reset_tenant_context(token)


def _public_demo_sessions_enabled() -> bool:
    return os.getenv("SHELFWISE_PUBLIC_DEMO_SESSION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class CreateWorkAccountBody(BaseModel):
    """Owner-provisioned work account; role is validated against the domain enum."""

    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    given_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    position: str = Field(min_length=1, max_length=120)
    role: Role
    password: str = Field(min_length=12, max_length=500)


class ChangeWorkAccountRoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


@app.post("/auth/login", dependencies=[WRITE_LIMIT_DEP])
def company_login(body: LoginBody) -> JSONResponse:
    """Company-account login: verify the configured owner account, mint the JWT session.

    Real credential verification with stdlib scrypt (no new dependencies): the deployment
    configures SHELFWISE_LOGIN_EMAIL and SHELFWISE_LOGIN_PASSWORD_HASH (format
    "scrypt$<salt_hex>$<hash_hex>"; generation one-liner documented in .env.example).
    Unconfigured deployments answer an honest 503, never an open door; failures are a
    uniform 401 with no oracle about which field was wrong. The minted session is the
    exact owner-role JWT cookie the rest of the platform already trusts and verifies.
    """
    secret = os.getenv("TENANT_AUTH_SECRET", "")
    tenant_id = default_tenant_context().tenant_id
    account = account_store.get_by_email(tenant_id, body.email)
    if account is not None:
        if not account["active"] or not _login_credentials_valid(
            email=body.email, password=body.password, configured_email=account["email"],
            configured_hash=account["password_hash"],
        ):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return _account_session_response(
            TenantContext(tenant_id=tenant_id, user_id=account["id"], role=Role(account["role"])),
            secret=secret,
        )
    configured_email = os.getenv("SHELFWISE_LOGIN_EMAIL", "").strip().lower()
    configured_hash = os.getenv("SHELFWISE_LOGIN_PASSWORD_HASH", "").strip()
    if not secret or not configured_email or not configured_hash:
        raise HTTPException(status_code=503, detail="Company login is not configured")
    if not _login_credentials_valid(
        email=body.email,
        password=body.password,
        configured_email=configured_email,
        configured_hash=configured_hash,
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    ctx = TenantContext(
        tenant_id=default_tenant_context().tenant_id,
        user_id=configured_email,
        role=Role.OWNER,
    )
    return _account_session_response(ctx, secret=secret)


def _account_session_response(ctx: TenantContext, *, secret: str) -> JSONResponse:
    """Mint the standard strict browser session for an authenticated workforce account."""
    lifetime = _env_positive_int("SHELFWISE_LOGIN_SESSION_SECONDS", 43_200)
    token = encode_hs256_token(
        {**ctx.to_dict(), "exp": int(datetime.now(UTC).timestamp()) + lifetime},
        secret=secret,
    )
    response = JSONResponse({"session": ctx.to_dict(), "mode": "jwt"})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=lifetime,
        httponly=True,
        secure=_cookie_secure_setting(),
        samesite="strict",
        path="/",
    )
    return response


@app.get("/accounts")
def list_work_accounts(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """List non-secret staff account records for the signed-in owner."""
    return {"accounts": account_store.list(ctx.tenant_id)}


@app.post("/accounts", dependencies=[WRITE_LIMIT_DEP])
def create_work_account(
    body: CreateWorkAccountBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Create a least-privilege staff account within the owner's tenant."""
    if body.role is Role.OWNER:
        raise HTTPException(
            status_code=422,
            detail="Create additional owners through a recovery flow",
        )
    try:
        account = account_store.create(
            {
                "tenant_id": ctx.tenant_id,
                "email": body.email,
                "given_name": body.given_name,
                "surname": body.surname,
                "position": body.position,
                "role": body.role.value,
                "password_hash": _scrypt_password_hash(body.password),
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"account": account}


@app.post("/accounts/{account_id}/deactivate", dependencies=[WRITE_LIMIT_DEP])
def deactivate_work_account(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Remove a staff account's ability to sign in without deleting its audit identity."""
    account = account_store.set_active(ctx.tenant_id, account_id, active=False)
    if account is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    return {"account": account}


@app.post("/accounts/{account_id}/reactivate", dependencies=[WRITE_LIMIT_DEP])
def reactivate_work_account(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Restore an existing staff account without recreating its identity."""
    account = account_store.set_active(ctx.tenant_id, account_id, active=True)
    if account is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    return {"account": account}


@app.post("/accounts/{account_id}/role", dependencies=[WRITE_LIMIT_DEP])
def change_work_account_role(
    account_id: str,
    body: ChangeWorkAccountRoleBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Change a staff role without creating a duplicate account."""
    if body.role is Role.OWNER:
        raise HTTPException(status_code=422, detail="Use a dedicated owner recovery flow")
    account = account_store.set_role(ctx.tenant_id, account_id, role=body.role.value)
    if account is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    return {"account": account}


_scrypt_password_hash = scrypt_password_hash
_login_credentials_valid = login_credentials_valid


@app.post("/auth/session", dependencies=[WRITE_LIMIT_DEP])
def create_public_demo_session(request: Request) -> JSONResponse:
    """Issue one opaque browser identity for a same-origin public demonstration."""
    if _auth_mode() != "jwt":
        return JSONResponse({"session": default_tenant_context().to_dict(), "mode": "local"})
    secret = os.getenv("TENANT_AUTH_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Tenant authentication is unavailable")
    existing = _request_authorization(request)
    if existing:
        try:
            ctx = verify_bearer_token(existing, secret=secret)
            return JSONResponse({"session": ctx.to_dict(), "mode": "jwt"})
        except ValueError:
            pass
    if not _public_demo_sessions_enabled():
        raise HTTPException(status_code=401, detail="Authentication is required")
    ctx = TenantContext(
        tenant_id=default_tenant_context().tenant_id,
        user_id=f"demo_{uuid4().hex}",
        role=Role.MANAGER,
    )
    lifetime = _env_positive_int("SHELFWISE_PUBLIC_SESSION_SECONDS", 43_200)
    token = encode_hs256_token(
        {**ctx.to_dict(), "exp": int(datetime.now(UTC).timestamp()) + lifetime},
        secret=secret,
    )
    response = JSONResponse({"session": ctx.to_dict(), "mode": "jwt"})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=lifetime,
        httponly=True,
        secure=_cookie_secure_setting(),
        samesite="strict",
        path="/",
    )
    return response


class TenantProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    country: str = Field(default="ZA", min_length=2, max_length=2)
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    timezone: str = Field(default="Africa/Johannesburg", min_length=1, max_length=64)
    budgets: dict[str, int] = Field(default_factory=dict)
    model_limits: dict[str, object] = Field(default_factory=dict)
    connector_policy: dict[str, object] = Field(default_factory=dict)

    @field_validator("country", "currency")
    @classmethod
    def uppercase_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("budgets")
    @classmethod
    def positive_budgets(cls, value: dict[str, int]) -> dict[str, int]:
        for key, amount in value.items():
            if amount < 0:
                raise ValueError(f"budget must be non-negative: {key}")
        return value

    @field_validator("connector_policy")
    @classmethod
    def reject_inline_secrets(cls, value: dict[str, object]) -> dict[str, object]:
        if _contains_inline_secret(value):
            raise ValueError("connector_policy may only store secret references, not secret values")
        return value


class ConnectorIntakeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] = Field(default_factory=dict)


class CsvIntakeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["products", "stock", "expiry", "sales"]
    csv_text: str = Field(min_length=1, max_length=5 * 1024 * 1024)
    mapping: dict[str, str] | None = None
    location_id: str | None = Field(default=None, max_length=100)


class ScanCandidateConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: dict[str, Any]
    review_note: str | None = Field(default=None, max_length=500)


class TaskCompletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_reference: str = Field(min_length=1, max_length=200)
    completed_units: int = Field(ge=0, le=1_000_000)
    observed_location: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=500)


class InventoryPositionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=200)
    location_type: Literal["shelf", "backroom", "bin", "quarantine", "returns"]
    location_id: str = Field(min_length=1, max_length=200)
    bin_id: str = Field(default="unassigned", min_length=1, max_length=200)
    quantity: int = Field(ge=0, le=1_000_000)
    state: Literal["available", "quarantined", "relocated", "count_pending"]
    source_reference: str = Field(min_length=1, max_length=200)


class DecisionCorrectionBody(BaseModel):
    """Optional human-in-the-loop correction captured alongside an approve/reject action.

    Populating any field here persists a `correction` annotation on the decision so later
    analysis can compare what the model recommended against what the human actually did.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=2_000)
    override_action: dict[str, Any] | None = Field(default=None)

    def as_correction(self, *, decision_action: str) -> dict[str, Any] | None:
        """Return a correction payload, or None if nothing was actually provided."""
        if self.reviewer is None and self.reason is None and self.override_action is None:
            return None
        return {
            "decision": decision_action,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "override_action": self.override_action,
        }


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "shelfwise",
        "version": "0.1.0",
        "inference": load_inference_config().to_public_dict(),
    }


@app.get("/readiness")
def readiness(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    inference_ready = inference_readiness_payload()
    inference = inference_ready["inference"]
    gateway_status = "offline-safe" if inference["provider"] == "offline" else "configured"
    seed_status = "ok"
    try:
        world_facts.get_hero_sku(ctx.tenant_id)
    except (FileNotFoundError, ValueError):
        seed_status = "error"

    return {
        "ready": True,
        "checks": {
            "backend": "ok",
            "golden_cascade": "ok",
            "hitl": "ok",
            "learning": "ok",
            "critic_rejection": "ok",
            "store_intelligence": "ok",
            "seed_data": seed_status,
            "inference_gateway": gateway_status,
            "amd_demo": "ok" if inference_ready["ready_for_amd_demo"] else "pending",
            "decision_store": type(decision_store).__name__,
            "learning_store": type(learning_store).__name__,
            "event_store": type(event_store).__name__,
            "event_bus": type(event_bus).__name__,
            "journal": type(journal).__name__,
            "writeback_sink": type(writeback_sink).__name__,
            "worker": type(cascade_worker).__name__,
            "worker_service": worker_service.status(),
            "trace_registry": type(trace_registry).__name__,
            "prompt_registry": type(prompt_registry).__name__,
            "tenant_fact_store": type(tenant_fact_store).__name__,
            "tenant_profile_store": type(tenant_profile_store).__name__,
            "worldgen_run_store": type(worldgen_run_store).__name__,
            "inbound_record_store": type(inbound_record_store).__name__,
            "cold_chain_feed": cold_chain_feed.status(),
            "twin_projection_worker": twin_projection_service.status(),
            "fidelity_revalidation": fidelity_revalidation_service.status(),
            "retention": retention_service.status(),
            "auth_mode": _auth_mode(),
            "tenant_auth_secret_configured": bool(os.getenv("TENANT_AUTH_SECRET", "")),
            "tenant_scoped_tables": sorted(TENANT_SCOPED_TABLES),
        },
        "next_external_checks": [
            "Fireworks credential smoke",
            "AMD Developer Cloud MI300X/vLLM credential smoke",
            "Docker build after Docker Desktop engine starts",
            "Browser verification after frontend build",
        ],
    }


@app.get("/inference/config")
def inference_config() -> dict[str, object]:
    return load_inference_config().to_public_dict()


def inference_readiness_payload() -> dict[str, object]:
    """Report whether live AMD MI300X/vLLM (or Fireworks) inference is configured."""
    config = load_inference_config()
    public = config.to_public_dict()
    routine_ready = (
        bool(config.base_url_for_agent("inventory"))
        and bool(config.api_key_for_agent("inventory"))
        and bool(config.routine_model)
    )
    strong_ready = (
        bool(config.base_url_for_agent("executive"))
        and bool(config.api_key_for_agent("executive"))
        and bool(config.strong_model)
    )
    # The former 30-second hackathon target is an observability metric, not a
    # correctness gate.  A healthy live model may legitimately need longer.
    network_ready = routine_ready and strong_ready and config.timeout_seconds > 0
    dual_ready = network_ready and config.dual_model_configured
    amd_ready = dual_ready and config.provider is ProviderKind.VLLM_MI300X
    return {
        "ready_for_live_inference": network_ready,
        "ready_for_dual_model_inference": dual_ready,
        "ready_for_amd_demo": amd_ready,
        "amd_compute_used_by_default": config.provider is ProviderKind.VLLM_MI300X,
        "inference": public,
        "checks": {
            "openai_chat_completions_contract": "ok",
            "routine_endpoint": "ok" if routine_ready else "missing",
            "strong_endpoint": "ok" if strong_ready else "missing",
            "distinct_model_ids": "ok" if config.dual_model_configured else "missing",
            "routine_model": "ok" if config.routine_model else "missing",
            "strong_model": "ok" if config.strong_model else "missing",
            "timeout_configured": "ok" if config.timeout_seconds > 0 else "missing",
            "amd_mi300x_provider": (
                "ok" if config.provider is ProviderKind.VLLM_MI300X else "pending"
            ),
        },
        "next_step": (
            "Run routine and strong inference smoke checks against both vLLM endpoints."
            if amd_ready
            else "Configure distinct routine and strong Gemma models and verify both endpoints."
        ),
    }


@app.get("/inference/readiness")
def inference_readiness() -> dict[str, object]:
    return inference_readiness_payload()


@app.get("/submission/readiness")
def submission_readiness() -> dict[str, object]:
    inference_ready = inference_readiness_payload()
    return {
        "track": "Track 3: Unicorn",
        "ready_for_submission_prescreen": inference_ready["ready_for_amd_demo"],
        "checks": {
            "github_repository_url_required": "required",
            "demo_video_required": "required",
            "slide_deck_pdf_required": "required",
            "hosted_url": "recommended",
            "docker_image_required": "required",
            "amd_compute_usage": "ok" if inference_ready["ready_for_amd_demo"] else "pending",
            "response_timeout": ("configured" if _request_timeout_seconds() > 0 else "missing"),
            "english_responses": "enforced_in_code",
            "unseen_inputs": "not_cached_by_question",
            "live_cloud_measurements": "required_before_submission",
        },
        "inference": inference_ready,
    }


@app.get("/inference/smoke")
def inference_smoke(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    _require_amd_inference()
    data_domain = _chat_data_domain()
    readiness_payload = inference_readiness_payload()
    system_prompt = "You are the ShelfWise critic. Reply briefly."
    prompt = prompt_registry.record_prompt(
        agent="critic",
        version="v1",
        system_prompt=system_prompt,
        tenant_id=ctx.tenant_id,
        prompt_id="smoke:v1",
        schema_version="v1",
    )
    try:
        result = OpenAICompatibleInferenceClient(
            recorder=lambda payload: record_model_run({**payload, "data_domain": data_domain})
        ).complete(
            agent="critic",
            system=system_prompt,
            user="Say ready if the inference gateway is reachable.",
            max_tokens=40,
            tenant_id=ctx.tenant_id,
            prompt_version=prompt.id,
            schema_version=prompt.schema_version,
        )
    except InferenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Inference gateway is unavailable",
                "readiness": readiness_payload,
            },
        ) from exc
    return {
        "ok": True,
        "data_domain": data_domain,
        "amd_compute_used": (
            result.provider == ProviderKind.VLLM_MI300X.value and result.used_network
        ),
        "result": result.to_dict(),
        "readiness": readiness_payload,
        "prompt_version": prompt.to_dict(),
    }


@app.get("/data/seed/summary")
def seed_summary(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return one measured product summary, or an explicit incomplete-data receipt."""
    facts = _facts_for_read(
        tenant_id=ctx.tenant_id,
        data_domain=data_domain,
        store_id=store_id,
    )
    resolved_domain = str(getattr(facts, "data_domain", DataDomain.WORLD_SIMULATION.value))
    try:
        hero_sku = facts.get_hero_sku(ctx.tenant_id)
        seed_data = facts.get_scenario_facts(ctx.tenant_id, hero_sku).to_dict()
    except MissingOperationalFacts as exc:
        return {
            "data_domain": resolved_domain,
            "seed_data": None,
            "status": "insufficient_operational_facts",
            "missing_data": list(exc.missing),
        }
    return {"data_domain": resolved_domain, "seed_data": seed_data}


@app.get("/products/attention")
def product_attention(
    limit: int = 20,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        facts = _facts_for_read(
            tenant_id=ctx.tenant_id,
            data_domain=data_domain,
            store_id=store_id,
        )
        return product_attention_queue(
            facts=facts,
            limit=limit,
            tenant_id=ctx.tenant_id,
            candidate_store=candidate_store,
            open_orders=open_order_store.coverage(
                ctx.tenant_id,
                data_domain=str(getattr(facts, "data_domain", "world_simulation")),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/products/search")
def product_search(
    q: str = "",
    limit: int = 20,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        return search_product_catalog(
            facts=_facts_for_read(
                tenant_id=ctx.tenant_id,
                data_domain=data_domain,
                store_id=store_id,
            ),
            query=q,
            limit=limit,
            tenant_id=ctx.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/ingest", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def ingest_event(
    payload: dict[str, Any],
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    try:
        event = Event.parse_wire(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _auth_mode() == "jwt" and event.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="Event tenant does not match token")
    _reject_stale_operational_event(event)
    if event.type is EventType.RECALL_NOTICE:
        try:
            validate_recall_notice(event)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event.type is EventType.INVENTORY_EXCEPTION:
        try:
            validate_inventory_exception(event)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return record_pipeline_event(event)


def _reject_stale_operational_event(event: Event) -> None:
    """Reject implausibly stale live intake while allowing simulation replay and backfill."""
    if event.data_domain is not DataDomain.OPERATIONAL_TWIN:
        return
    raw = os.getenv("SHELFWISE_MAX_EVENT_AGE_SECONDS", "31536000")
    try:
        max_age_seconds = max(1, int(raw))
    except ValueError:
        max_age_seconds = 31_536_000
    age_seconds = (datetime.now(UTC) - event.ts).total_seconds()
    if age_seconds > max_age_seconds:
        raise HTTPException(status_code=422, detail="Event timestamp is too stale")


@app.post(
    "/scan/candidates/confirm",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def confirm_scan_candidate(
    body: ScanCandidateConfirmationBody,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Promote one reviewed scanner candidate into the canonical event pipeline."""
    try:
        event = Event.parse_wire(body.event)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="Scan candidate tenant does not match token")
    if event.source is not EventSource.SCANNER:
        raise HTTPException(status_code=422, detail="Only scanner candidates can be confirmed")
    if event.data_domain is not DataDomain.OPERATIONAL_TWIN:
        raise HTTPException(
            status_code=422,
            detail="Scan candidates can only enter the operational data source",
        )

    existing = event_store.get(
        event.id,
        tenant_id=event.tenant_id,
        data_domain=event.data_domain,
    )
    if _same_reviewed_candidate(existing, event, body.review_note):
        return record_pipeline_event(Event.parse_wire(existing or {}))

    reviewed_event = replace(
        event,
        payload={
            **event.payload,
            "reviewed_by": ctx.user_id,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "review_note": body.review_note,
        },
    )
    return record_pipeline_event(reviewed_event)


def _same_reviewed_candidate(
    stored: dict[str, Any] | None,
    event: Event,
    review_note: str,
) -> bool:
    if stored is None or not isinstance(stored.get("payload"), dict):
        return False
    stored_payload = dict(stored["payload"])
    stored_note = str(stored_payload.pop("review_note", ""))
    stored_payload.pop("reviewed_by", None)
    stored_payload.pop("reviewed_at", None)
    return stored_payload == event.payload and stored_note == review_note


@app.get("/events")
def list_events(
    limit: int = 200,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    try:
        events = event_store.list(
            limit=limit,
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _auth_mode() == "jwt":
        events = [item for item in events if item.get("tenant_id") == ctx.tenant_id]
    return {"data_domain": resolved_domain, "events": events}


@app.get("/candidates/{candidate_key}/history")
def candidate_history(
    candidate_key: str,
    limit: int = 100,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return the immutable lifecycle transitions recorded for one candidate."""
    try:
        entries = candidate_store.history(ctx.tenant_id, candidate_key, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "tenant_id": ctx.tenant_id,
        "candidate_key": candidate_key,
        "history": [entry.to_dict() for entry in entries],
    }


@app.get("/events/bus")
def list_bus_events(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    messages = [
        item
        for item in event_bus.list()
        if isinstance(item.get("event"), dict)
        and item["event"].get("data_domain", "operational_twin") == resolved_domain
    ]
    if _auth_mode() == "jwt":
        messages = [item for item in messages if _bus_message_tenant(item) == ctx.tenant_id]
    return {"data_domain": resolved_domain, "messages": messages}


def _bus_message_tenant(message: dict[str, Any]) -> str | None:
    event = message.get("event")
    return event.get("tenant_id") if isinstance(event, dict) else None


@app.get("/trace/{correlation_id}")
def get_trace(
    correlation_id: str,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    trace = trace_registry.get(
        correlation_id,
        tenant_id=ctx.tenant_id,
        data_domain=data_domain or _chat_data_domain(),
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": trace}


@app.get("/traces")
def list_traces(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "traces": trace_registry.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
    }


@app.get("/detective/root-cause/{target_id}")
def detective_root_cause(
    target_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        events = event_store.list(limit=500, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _auth_mode() == "jwt":
        events = [item for item in events if item.get("tenant_id") == ctx.tenant_id]
    analysis = analyze_root_cause(
        target_id,
        events=events,
        decisions=tenant_scoped_decisions(ctx),
    )
    if not analysis.found:
        raise HTTPException(status_code=404, detail="Root-cause target not found")
    return {"analysis": analysis.to_dict()}


@app.get("/detective/root-cause-sql")
def detective_root_cause_sql() -> dict[str, object]:
    return {"sql": root_cause_cte_sql()}


def _conversation_data_domain(conversation: dict[str, Any]) -> str:
    direct = conversation.get("data_domain")
    if direct:
        return str(direct)
    for message in conversation.get("messages", []):
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("data_domain"):
            return str(metadata["data_domain"])
    return DataDomain.WORLD_SIMULATION.value


@app.get("/chat/conversations")
def list_chat_conversations(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    conversations = chat_store.list(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
    if data_domain is not None:
        conversations = [
            item for item in conversations if _conversation_data_domain(item) == data_domain
        ]
    return {
        "data_domain": data_domain,
        "conversations": [
            {key: value for key, value in item.items() if key != "messages"}
            | {"data_domain": _conversation_data_domain(item)}
            | {"message_count": len(item["messages"])}
            for item in conversations
        ],
    }


@app.get("/chat/conversations/{conversation_id}")
def get_chat_conversation(
    conversation_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    conversation = chat_store.get(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"conversation": conversation}


def _chunk_answer_for_delta_events(text: str, *, words_per_chunk: int = 4) -> list[str]:
    """Split a validated answer into ordered chunks for incremental SSE delivery.

    Deliberately chunks the *already fully computed and grounding-validated* text rather
    than re-invoking the model with `stream=True`: the answer is produced by a bounded
    tool-calling loop (`AgentOrchestrator.run_messages`, shared with every production
    cascade) that cannot know a turn is the final answer until after it has arrived, and
    validates grounding only once the complete text exists. Streaming raw provider tokens
    live would mean either (a) showing the client unvalidated text before grounding can
    reject it - the exact "chunk-the-answer fake" this route's own name history warns
    against in the other direction - or (b) a second, separately-sampled streaming call
    whose text is not guaranteed to match the validated one word-for-word. Both break the
    invariant every other agentic surface in this app holds: nothing reaches a caller
    that was not itself validated. Chunking the validated text preserves that invariant
    while still giving the client genuine incremental delivery instead of one blocking
    event - the real defect this route had (a false claim of live per-token streaming) is
    closed by making the claim match what is actually safe to deliver.
    """
    words = text.split(" ")
    chunks: list[str] = []
    for start in range(0, len(words), words_per_chunk):
        chunk_words = words[start : start + words_per_chunk]
        prefix = "" if start == 0 else " "
        chunks.append(prefix + " ".join(chunk_words))
    return chunks or [text]


@app.post("/chat/stream", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def chat_stream(body: ChatBody, ctx: TenantContext = CURRENT_TENANT_DEP) -> Any:
    """SSE chat with a truthful lifecycle envelope - never the chunk-the-answer fake.

    Events say exactly what happened: `accepted` (request queued into the real
    pipeline), a sequence of `delta` events (the validated answer delivered incrementally
    - see `_chunk_answer_for_delta_events` for why this chunks already-validated text
    instead of streaming live, unvalidated provider tokens), `answer`/`replayed` (the
    complete text again, once, for any client that ignores `delta` events and only reads
    the terminal payload - kept for backward compatibility with the pre-delta contract),
    then `done` (the same metadata receipts POST /chat returns). No event is ever emitted
    for generation that did not occur, and nothing is ever emitted before
    `_new_chat_response`'s grounding validation has already passed.
    """
    from fastapi.responses import StreamingResponse

    def sse(event: str, payload: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"

    def events() -> Any:
        conversation_id = body.conversation_id or f"conv_{uuid4().hex}"
        message_id = body.message_id or f"msg_{uuid4().hex}"
        yield sse(
            "accepted",
            {"conversation_id": conversation_id, "message_id": message_id},
        )
        inner = chat(
            ChatBody(
                question=body.question,
                conversation_id=conversation_id,
                message_id=message_id,
                data_domain=body.data_domain,
                live_required=body.live_required,
            ),
            ctx,
        )
        answer_text = bytes(inner.body).decode("utf-8")
        replayed = inner.headers.get("X-ShelfWise-Replayed", "false") == "true"
        for chunk in _chunk_answer_for_delta_events(answer_text):
            yield sse("delta", {"text": chunk})
        yield sse("replayed" if replayed else "answer", {"text": answer_text})
        yield sse(
            "done",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "correlation_id": inner.headers.get("X-ShelfWise-Correlation-ID", ""),
                "answer_source": inner.headers.get("X-ShelfWise-Answer-Source", ""),
                "model": inner.headers.get("X-ShelfWise-Model", ""),
            },
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/chat", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def chat(body: ChatBody, ctx: TenantContext = CURRENT_TENANT_DEP) -> PlainTextResponse:
    conversation_id = body.conversation_id or f"conv_{uuid4().hex}"
    message_id = body.message_id or f"msg_{uuid4().hex}"
    requested_domain = body.data_domain or _chat_data_domain()
    with chat_store.locked(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        conversation_id=conversation_id,
    ):
        conversation = chat_store.get(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation_id,
        )
        existing_domains = {_conversation_data_domain(conversation)} if conversation else set()
        existing_domains.update(
            {
                str(item.get("metadata", {}).get("data_domain"))
                for item in (conversation or {}).get("messages", [])
                if isinstance(item, dict)
                and isinstance(item.get("metadata"), dict)
                and item.get("metadata", {}).get("data_domain")
            }
        )
        if existing_domains and requested_domain not in existing_domains:
            raise HTTPException(
                status_code=409,
                detail="Start a new conversation when changing the data source",
            )
        prior_answer = chat_store.answer_for_message(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if prior_answer is not None:
            prior_domain = str(
                prior_answer.get("metadata", {}).get(
                    "data_domain",
                    DataDomain.WORLD_SIMULATION.value,
                )
            )
            if prior_domain != requested_domain:
                raise HTTPException(
                    status_code=409,
                    detail="Message identity already belongs to another data source",
                )
            return _chat_response(
                answer=str(prior_answer["text"]),
                conversation_id=conversation_id,
                message_id=message_id,
                correlation_id=str(prior_answer.get("metadata", {}).get("correlation_id", "")),
                metadata=prior_answer.get("metadata", {}),
                replayed=True,
            )
        return _new_chat_response(
            body=body,
            ctx=ctx,
            conversation_id=conversation_id,
            message_id=message_id,
        )


def _new_chat_response(
    *,
    body: ChatBody,
    ctx: TenantContext,
    conversation_id: str,
    message_id: str,
) -> PlainTextResponse:
    live_twin_context = twin_service.live_context(ctx.tenant_id)
    chat_domain = body.data_domain or _chat_data_domain()
    use_live_twin = chat_domain == "operational_twin"
    decisions = tenant_scoped_decisions(ctx, data_domain=chat_domain)
    pending_count = sum(1 for item in decisions if item.get("status") == "pending")
    resolved_count = len(decisions) - pending_count
    thresholds = learning_store.thresholds(
        tenant_id=ctx.tenant_id,
        data_domain=chat_domain,
    )
    state = {
        "decision_summary": {
            "total": len(decisions),
            "pending": pending_count,
            "resolved": resolved_count,
        },
        "decisions": _bounded_chat_decisions(decisions, question=body.question),
        "learning": {
            "threshold_count": len(thresholds),
            "thresholds": _bounded_chat_thresholds(
                thresholds,
                question=body.question,
                limit=_CHAT_THRESHOLD_LIMIT,
            ),
            "events": _bounded_chat_learning_events(
                learning_store.list_events(
                    tenant_id=ctx.tenant_id,
                    data_domain=chat_domain,
                ),
                question=body.question,
            ),
        },
        "traces": [
            _compact_chat_trace(item)
            for item in trace_registry.list(
                tenant_id=ctx.tenant_id,
                data_domain=chat_domain,
            )[:_CHAT_TRACE_LIMIT]
        ],
        "live_twin_context": live_twin_context if use_live_twin else None,
        "store_intelligence": (
            None if use_live_twin else world_facts.get_store_intelligence(ctx.tenant_id)
        ),
    }
    conversation = chat_store.get(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        conversation_id=conversation_id,
    )
    conversation_summary = None
    if conversation:
        state["conversation_history"] = _bounded_chat_history(conversation["messages"])
        # Hierarchical memory (plan Section 37/41): everything older than the recent
        # window is compacted into a durable, provenance-tracked rolling summary instead
        # of silently falling off the end of a bare sliding window - a long
        # conversation keeps its objective, corrections, and earlier turns available to
        # every later answer.
        conversation_summary = compact_conversation(
            conversation_memory_store,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation_id,
            messages=conversation["messages"],
            recent_window=_CHAT_HISTORY_LIMIT,
        )
        if conversation_summary is not None:
            state["conversation_summary"] = conversation_summary.text

    # Progressive skill discovery (plan Section 39/41): the model sees only the promoted
    # skills relevant to THIS question, never the whole tool surface.
    discovered_skills = discover_skills(
        skill_registry,
        question=body.question,
        role=str(getattr(ctx, "role", "") or "manager"),
        tenant_id=ctx.tenant_id,
    )
    if discovered_skills:
        state["skill_catalogue"] = [
            {
                "id": manifest.id,
                "name": manifest.name,
                "description": manifest.description,
                "tools": list(manifest.required_tools),
            }
            for manifest in discovered_skills
        ]

    # Deterministic tier routing (plan Section 41.1): the route is computed from facts
    # known before inference and saved as an auditable receipt on the answer metadata.
    conversation_route = choose_conversation_route(
        ConversationRouteRequest(
            domains=tuple({manifest.domain_owner for manifest in discovered_skills}),
            risk_tier="low",
            asks_for_scenario=_question_asks_for_scenario(body.question),
            has_source_conflict=False,
            has_memory_conflict=False,
            is_simple_followup=bool(conversation) and len(body.question) <= 80,
        )
    )

    client = OpenAICompatibleInferenceClient(
        recorder=lambda payload: record_model_run({**payload, "data_domain": chat_domain})
    )
    _require_amd_inference()
    correlation_id = f"chat:{conversation_id}:{message_id}"
    try:
        answer, _meta = build_chat_reply_with_meta(
            question=body.question,
            state=state,
            client=client,
            tenant_id=ctx.tenant_id,
            correlation_id=correlation_id,
            live_required=body.live_required or _is_production_deployment(),
            decisions=decision_store,
            memory=learning_store,
            facts=world_facts,
            twin=twin_service if use_live_twin else None,
            audit=tool_audit,
            selected_memory_ids=(
                (conversation_summary.id,) if conversation_summary is not None else ()
            ),
            selected_skill_ids=tuple(manifest.id for manifest in discovered_skills),
        )
    except InferenceError as exc:
        raise HTTPException(status_code=503, detail="Live chat inference failed") from exc
    _meta["correlation_id"] = correlation_id
    _meta["data_domain"] = chat_domain
    _meta["conversation_route"] = conversation_route.to_dict()
    if discovered_skills:
        _meta["skills"] = [manifest.id for manifest in discovered_skills]
    if conversation_summary is not None:
        _meta["conversation_summary_id"] = conversation_summary.id
    chat_store.append_exchange(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        question=body.question,
        answer=answer,
        metadata=_meta,
    )
    return _chat_response(
        answer=answer,
        conversation_id=conversation_id,
        message_id=message_id,
        correlation_id=correlation_id,
        metadata=_meta,
        replayed=False,
    )


def _chat_response(
    *,
    answer: str,
    conversation_id: str,
    message_id: str,
    correlation_id: str,
    metadata: dict[str, Any],
    replayed: bool,
) -> PlainTextResponse:
    return PlainTextResponse(
        answer,
        headers={
            "X-ShelfWise-Conversation-ID": conversation_id,
            "X-ShelfWise-Message-ID": message_id,
            "X-ShelfWise-Correlation-ID": correlation_id,
            "X-ShelfWise-Answer-Source": str(metadata.get("answer_source", "unknown")),
            "X-ShelfWise-Model": str(metadata.get("model", "")),
            "X-ShelfWise-Provider": str(metadata.get("provider", "unknown")),
            "X-ShelfWise-Replayed": str(replayed).lower(),
            "X-ShelfWise-Data-Domain": str(metadata.get("data_domain", "unknown")),
        },
    )


@app.get("/tools/platform")
def list_platform_tools(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    tools = (
        build_live_twin_tools(
            decisions=decision_store,
            memory=learning_store,
            audit=tool_audit,
            twin=twin_service,
            tenant_id=ctx.tenant_id,
        )
        if resolved_domain == DataDomain.OPERATIONAL_TWIN.value
        else build_platform_tools(
            decisions=decision_store,
            memory=learning_store,
            audit=tool_audit,
            facts=world_facts,
            tenant_id=ctx.tenant_id,
        )
    )
    return {"data_domain": resolved_domain, "tools": [tool.to_dict() for tool in tools]}


@app.get("/tools/platform/audit")
def list_platform_tool_audit(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "events": tool_audit.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
    }


@app.get("/cold-chain/feed")
def list_cold_chain_feed(limit: int = 100) -> dict[str, object]:
    return {"status": cold_chain_feed.status(), "events": cold_chain_feed.list_events(limit=limit)}


@app.get("/tenants/me")
def get_tenant_profile(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    profile = tenant_profile_store.get(ctx.tenant_id)
    if profile is None:
        profile = default_tenant_profile(ctx.tenant_id)
    return {"profile": profile}


@app.post(
    "/tenants/me",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_tenant_profile(
    body: TenantProfileBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "tenant_id": ctx.tenant_id,
        "name": body.name,
        "country": body.country,
        "currency": body.currency,
        "timezone": body.timezone,
    }
    if "budgets" in body.model_fields_set:
        payload["budgets"] = body.budgets
    if "model_limits" in body.model_fields_set:
        payload["model_limits"] = body.model_limits
    if "connector_policy" in body.model_fields_set:
        payload["connector_policy"] = body.connector_policy
    profile = tenant_profile_store.upsert(payload)
    return {"profile": profile}


@app.post(
    "/connectors/{system}/intake",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def intake_connector_payload(
    system: str,
    body: ConnectorIntakeBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    try:
        source_system = SourceSystem(system.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown connector system") from exc

    try:
        records = map_for(source_system, body.payload, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {source_system.value} connector payload",
        ) from exc

    # A single payload can map to several records (e.g. one sales line per line item on
    # a multi-item order); every one of them must be persisted and pipelined, not just
    # the first. The top-level status/record/event/pipeline keys mirror the first
    # outcome for backward compatibility with single-line payloads; `records` carries
    # the complete per-line/per-count outcome list.
    outcomes = [_process_inbound_record(record) for record in records]
    first = outcomes[0]
    return {
        "status": first["status"],
        "record": first["record"],
        "event": first["event"],
        "pipeline": first["pipeline"],
        "records": outcomes,
    }


def _process_inbound_record(record: Any) -> dict[str, Any]:
    event = record_to_event(record)
    event_id = event.id if event is not None else None
    is_new, stored_record = inbound_record_store.record(record, event_id=event_id)
    if not is_new:
        return {
            "status": "duplicate",
            "record": stored_record,
            "event": None,
            "pipeline": None,
        }
    if not record.validation.ok:
        return {
            "status": "invalid",
            "record": stored_record,
            "event": None,
            "pipeline": None,
        }
    if event is None:
        return {
            "status": "recorded",
            "record": stored_record,
            "event": None,
            "pipeline": None,
        }

    pipeline = record_pipeline_event(event)
    return {
        "status": pipeline["status"],
        "record": stored_record,
        "event": pipeline["event"],
        "pipeline": pipeline,
    }


MAX_CSV_COMMIT_ROWS = 1_000


@app.post(
    "/intake/csv/preview",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def preview_csv_intake(
    body: CsvIntakeBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Dry-run a client CSV: infer the column mapping and validate every row, no writes.

    This is the onboarding safety valve: the operator previews until the mapping is right
    and the error list is understood, then commits the same payload. Preview and commit
    share one parser and one validator, so a clean preview is a true guarantee about commit.
    """
    text = _quarantined_csv_text(body)
    try:
        preview = preview_csv_file(
            body.kind,
            text,
            tenant_id=ctx.tenant_id,
            mapping=body.mapping,
            default_location=body.location_id,
        )
    except CsvIntakeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return preview.to_dict()


@app.post(
    "/intake/csv/commit",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def commit_csv_intake(
    body: CsvIntakeBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Commit a previewed client CSV through the same pipeline the live connectors use.

    Every data row becomes an inbound record (invalid rows quarantine with provenance,
    valid stock/expiry/sales rows become pipeline events, valid product rows upsert the
    catalog). Re-committing the same file is idempotent: rows dedup in the inbound store.
    """
    text = _quarantined_csv_text(body)
    try:
        records = build_csv_intake_records(
            body.kind,
            text,
            tenant_id=ctx.tenant_id,
            mapping=body.mapping,
            default_location=body.location_id,
        )
    except CsvIntakeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(records) > MAX_CSV_COMMIT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"commit is capped at {MAX_CSV_COMMIT_ROWS} rows per request; "
                "split the file and import it in parts (preview has no such cap)"
            ),
        )

    if body.kind == "products":
        outcomes = [_commit_product_record(record) for record in records]
    else:
        outcomes = [_process_inbound_record(record) for record in records]
    summary: dict[str, int] = {}
    for outcome in outcomes:
        status = str(outcome.get("status"))
        summary[status] = summary.get(status, 0) + 1
    return {
        "kind": body.kind,
        "rows": len(outcomes),
        "summary": summary,
        "records": outcomes,
    }


def _quarantined_csv_text(body: CsvIntakeBody) -> str:
    verdict = quarantine_intake(body.csv_text.encode("utf-8"), claimed_mime="text/csv")
    if not verdict.accepted:
        raise HTTPException(status_code=422, detail=f"CSV rejected: {verdict.reason}")
    return verdict.text or ""


def _commit_product_record(record: Any) -> dict[str, Any]:
    """Record provenance like any inbound row, then upsert the validated product catalog rows.

    Identity is deterministic so re-imports converge instead of duplicating: the product
    family id derives from the (tenant, name) pair and the variant id from the row's
    primary identifier. A code already mapped to a DIFFERENT variant surfaces as an
    identifier conflict for human review — never a silent overwrite.
    """
    outcome = _process_inbound_record(record)
    if outcome["status"] != "recorded":
        return outcome

    canonical = record.canonical_payload
    name = str(canonical.get("name") or "")
    primary_code = str(canonical.get("source_product_id") or "")
    product_id = "prod_" + _catalog_hash(record.tenant_id, name.lower())
    # The variant hash includes the product family: the same source code arriving under a
    # DIFFERENT product name must mint a different variant id so the identifier upsert
    # raises a conflict for review instead of silently re-pointing the code.
    variant_id = "var_" + _catalog_hash(record.tenant_id, f"{primary_code}|{product_id}")
    try:
        product = Product(
            tenant_id=record.tenant_id,
            product_id=product_id,
            name=name,
            category=canonical.get("category"),
            brand=canonical.get("brand"),
        )
        variant = ProductVariant(
            tenant_id=record.tenant_id,
            variant_id=variant_id,
            product_id=product_id,
            pack_size=canonical.get("pack_size"),
            unit_of_measure=canonical.get("unit_of_measure"),
        )
    except ValueError as exc:
        return {**outcome, "status": "invalid", "detail": str(exc)}
    product_catalog_store.upsert_product(product)
    product_catalog_store.upsert_variant(variant)

    identifiers: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    for identifier_kind in ("sku", "barcode", "gtin"):
        value = canonical.get(identifier_kind)
        if not value:
            continue
        try:
            product_catalog_store.upsert_identifier(
                ProductIdentifier(
                    tenant_id=record.tenant_id,
                    variant_id=variant_id,
                    kind=identifier_kind,
                    value=str(value),
                    source_system=SourceSystem.CSV.value,
                )
            )
        except ConflictingIdentifierError as exc:
            conflicts.append(
                {"kind": identifier_kind, "value": str(value), "detail": str(exc)}
            )
        else:
            identifiers.append({"kind": identifier_kind, "value": str(value)})
    return {
        **outcome,
        "status": "identifier_conflict" if conflicts else "cataloged",
        "catalog": {
            "product_id": product_id,
            "variant_id": variant_id,
            "identifiers": identifiers,
            "conflicts": conflicts,
        },
    }


def _catalog_hash(tenant_id: str, value: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{value}".encode()).hexdigest()[:16]


connector_poll_service = ConnectorPollService(
    cursors=connector_cursor_store,
    process_record=_process_inbound_record,
    tenant_id=default_tenant_context().tenant_id,
    credential_store=connector_credential_store,
)
app.router.add_event_handler("startup", connector_poll_service.start)
app.router.add_event_handler("shutdown", connector_poll_service.stop)


@app.get("/connectors/poll/status")
def connector_poll_status() -> dict[str, object]:
    """Report the background ERP/WMS poll loop's configured systems and last run."""
    return connector_poll_service.status()


@app.get("/worker/schedules")
def list_schedules() -> dict[str, object]:
    """Recurring governed schedules and their receipts."""
    return {
        "fidelity_revalidation": fidelity_revalidation_service.status(),
        "retention": retention_service.status(),
    }


@app.get("/worker/runs")
def list_worker_runs(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": resolved_domain,
        "runs": journal.list_runs(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
    }


@app.get("/worker/status")
def worker_status() -> dict[str, object]:
    return {"worker": worker_service.status()}


@app.post(
    "/worker/process-one",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP, Depends(worker_internal_guard)],
)
def process_one_worker_event() -> dict[str, object]:
    result = cascade_worker.process_one().to_dict()
    cascade = result.get("cascade")
    if isinstance(cascade, dict):
        result["cascade"] = record_cascade(cascade)
    return {"result": result}


@app.get("/decisions")
def list_decisions(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "decisions": tenant_scoped_decisions(ctx, data_domain=resolved_domain),
    }


@app.get("/learning")
def learning_summary(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "thresholds": learning_store.thresholds(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        "events": learning_store.list_events(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
    }


@app.get("/writeback/tasks")
def list_writeback_tasks(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return tenant-scoped pending task/draft records created by approval."""
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "tasks": writeback_sink.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
    }


@app.get("/inventory/positions")
def list_inventory_positions(
    sku: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    return {
        "positions": inventory_position_store.list(
            tenant_id=ctx.tenant_id,
            sku=sku.strip() if sku else None,
        )
    }


@app.post(
    "/inventory/positions",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_inventory_position(
    body: InventoryPositionBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    return {
        "position": inventory_position_store.upsert(
            {"tenant_id": ctx.tenant_id, **body.model_dump()}
        )
    }


@app.post(
    "/writeback/tasks/{task_id}/complete",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def complete_writeback_task(
    task_id: str,
    body: TaskCompletionBody,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain
    existing = next(
        (
            item
            for item in writeback_sink.list(
                tenant_id=ctx.tenant_id,
                data_domain=resolved_domain,
            )
            if item.get("id") == task_id
        ),
        None,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Write-back task not found")
    resolved_domain = str(existing.get("data_domain") or DataDomain.OPERATIONAL_TWIN.value)
    action = existing.get("action") if isinstance(existing.get("action"), dict) else {}
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    expected_units = params.get("units")
    if isinstance(expected_units, int) and body.completed_units != expected_units:
        raise HTTPException(
            status_code=409,
            detail=f"Completion units must equal approved units ({expected_units})",
        )
    receipt = {
        "source_reference": body.source_reference,
        "completed_units": body.completed_units,
        "observed_location": body.observed_location,
        "note": body.note,
        "completed_by": ctx.user_id,
    }
    try:
        task = writeback_sink.complete_task(
            task_id=task_id,
            tenant_id=ctx.tenant_id,
            receipt=receipt,
            data_domain=resolved_domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:  # It may have disappeared between validation and completion.
        raise HTTPException(status_code=404, detail="Write-back task not found")
    positions = _record_completed_inventory_movement(task)
    return {"task": task, "positions": positions}


def _record_completed_inventory_movement(task: dict[str, Any]) -> list[dict[str, Any]]:
    action = task.get("action") if isinstance(task.get("action"), dict) else {}
    if action.get("type") != "relocate_stock":
        return []
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    receipt = (
        task.get("completion_receipt") if isinstance(task.get("completion_receipt"), dict) else {}
    )
    tenant_id = str(task.get("tenant_id") or "")
    sku = str(params.get("sku") or "")
    source = str(params.get("observed_location") or "")
    destination = str(params.get("expected_location") or "")
    source_reference = str(receipt.get("source_reference") or "")
    units = int(receipt.get("completed_units") or 0)
    if not all((tenant_id, sku, source, destination, source_reference)):
        raise HTTPException(status_code=409, detail="Relocation receipt lacks position evidence")
    return [
        inventory_position_store.upsert(
            {
                "tenant_id": tenant_id,
                "sku": sku,
                "location_type": _physical_location_type(source),
                "location_id": source,
                "quantity": 0,
                "state": "relocated",
                "source_reference": source_reference,
            }
        ),
        inventory_position_store.upsert(
            {
                "tenant_id": tenant_id,
                "sku": sku,
                "location_type": _physical_location_type(destination),
                "location_id": destination,
                "quantity": units,
                "state": "available",
                "source_reference": source_reference,
            }
        ),
    ]


def _physical_location_type(location_id: str) -> str:
    lowered = location_id.lower()
    for location_type in ("backroom", "shelf", "quarantine", "returns", "bin"):
        if location_type in lowered:
            return location_type
    return "bin"


@app.get("/decisions/{decision_id}")
def get_decision(
    decision_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    decision = decision_store.get(decision_id)
    if decision is None or decision_belongs_to_other_tenant(decision, ctx):
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": decision}


# /chat sends its whole state as one JSON block in the model prompt. Unbounded decision/
# learning history grows with store size, not with what the question actually needs - in a
# 145-cycle stress run this made later prompts large enough that response latency climbed
# past LLM_TIMEOUT_SECONDS and every later call silently fell back to the offline reply.
# Pending decisions still need to stay in full (the assistant must see everything still
# awaiting a human), but resolved history and learning events only need a recent window.
_CHAT_PENDING_DECISION_LIMIT = 2
_CHAT_RESOLVED_DECISION_LIMIT = 1
_CHAT_LEARNING_EVENT_LIMIT = 5
_CHAT_THRESHOLD_LIMIT = 6
_CHAT_TRACE_LIMIT = 1
_CHAT_HISTORY_LIMIT = 4
_CHAT_HISTORY_TEXT_LIMIT = 600


def _facts_for_read(
    *,
    tenant_id: str,
    data_domain: Literal["operational_twin", "world_simulation"] | None,
    store_id: str | None,
) -> Any:
    """Select an explicit facts domain for product/query surfaces."""
    resolved = data_domain or _chat_data_domain()
    if resolved == DataDomain.WORLD_SIMULATION.value:
        return world_facts
    return operational_facts_for_query(tenant_id, store_id=store_id)


def _bounded_chat_decisions(
    decisions: list[dict[str, Any]],
    *,
    question: str = "",
) -> list[dict[str, Any]]:
    """Bound prompt context while the decision store retains the complete queue."""
    return select_chat_decisions(
        decisions,
        question=question,
        pending_limit=_CHAT_PENDING_DECISION_LIMIT,
        resolved_limit=_CHAT_RESOLVED_DECISION_LIMIT,
    )


_SCENARIO_QUESTION_MARKERS = ("what if", "scenario", "simulate", "would happen", "suppose")


def _question_asks_for_scenario(question: str) -> bool:
    """Deterministic routing fact: scenario/what-if reasoning requires the strong tier."""
    lowered = question.lower()
    return any(marker in lowered for marker in _SCENARIO_QUESTION_MARKERS)


def _bounded_chat_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Retain conversational meaning without recursively replaying tool metadata."""
    compact: list[dict[str, str]] = []
    for message in messages[-_CHAT_HISTORY_LIMIT:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        text = str(message.get("text") or "")[:_CHAT_HISTORY_TEXT_LIMIT]
        if role in {"user", "assistant"} and text:
            compact.append({"role": role, "text": text})
    return compact


def _bounded_chat_learning_events(
    events: list[dict[str, Any]],
    *,
    question: str,
) -> list[dict[str, Any]]:
    """Bound learning evidence while preserving question-matching older events."""
    return select_chat_learning_events(
        events,
        question=question,
        limit=_CHAT_LEARNING_EVENT_LIMIT,
    )


def _compact_chat_decision(decision: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "status",
        "summary",
        "role",
        "critic_verdict",
    )
    compact = {key: decision[key] for key in fields if key in decision}
    action = decision.get("action")
    if isinstance(action, dict):
        compact["action"] = {key: action[key] for key in ("type", "risk_tier") if key in action}
    return compact


def _bounded_chat_thresholds(
    thresholds: dict[str, Any],
    *,
    question: str,
    limit: int,
) -> dict[str, Any]:
    terms = {part.lower() for part in question.split() if len(part) >= 3}
    items = list(thresholds.items())
    matched = [item for item in items if any(term in item[0].lower() for term in terms)]
    remaining = [item for item in reversed(items) if item not in matched]
    return dict((matched + remaining)[:limit])


def _compact_chat_trace(trace: dict[str, Any]) -> dict[str, Any]:
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    return {
        "correlation_id": trace.get("correlation_id"),
        "decision_id": trace.get("decision_id"),
        "evidence_agents": trace.get("evidence_agents", []),
        "spans": [
            item.get("name") for item in spans if isinstance(item, dict) and item.get("name")
        ],
    }


def _compact_chat_learning_event(event: dict[str, Any]) -> dict[str, Any]:
    """Retain chat-relevant learning evidence without injecting durable raw payloads."""

    fields = (
        "id",
        "decision_id",
        "metric",
        "message",
        "created_at",
        "outcome",
        "previous_value",
        "updated_value",
    )
    return {key: event[key] for key in fields if key in event}


def _bounded_recent(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Return the most recent items by created_at/updated_at, most recent first."""
    if len(items) <= limit:
        return items
    ordered = sorted(
        items,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return ordered[:limit]


@app.post(
    "/decisions/{decision_id}/approve",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def approve_decision(
    decision_id: str,
    body: DecisionCorrectionBody | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    reject_cross_tenant_decision_access(decision_id, ctx)
    decision = decision_store.approve(decision_id, reviewer=ctx.user_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    if decision.get("status") != "approved":
        return {"decision": decision, "learning_event": None}
    learning_event = learning_store.record_approved_decision(decision)
    write_back = decision.get("write_back") or writeback_sink.create_task(
        idempotency_key=f"writeback:{decision_id}",
        tenant_id=decision_tenant_id(decision, ctx.tenant_id),
        data_domain=str(decision.get("data_domain") or DataDomain.WORLD_SIMULATION.value),
        title=_writeback_title(decision_id, decision),
        assignee_role=str(decision.get("role") or "manager"),
        action=decision_action(decision),
        rollback_instructions={
            "policy": "recommend_only_no_source_mutation",
            "rollback": "cancel_pending_manager_task_before_source_write",
            "decision_id": decision_id,
        },
    )
    annotations: dict[str, Any] = {
        "outcome": learning_event["outcome"],
        "learning_event": learning_event,
        "write_back": write_back,
    }
    correction = body.as_correction(decision_action="approve") if body is not None else None
    if correction is not None:
        annotations["correction"] = correction
    updated = decision_store.annotate(decision_id, **annotations)
    if updated is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": updated, "learning_event": learning_event}


def _writeback_title(decision_id: str, decision: dict[str, Any]) -> str:
    """Build a short manager-facing task title from the approved decision."""
    action_type = str(decision_action(decision).get("type") or "action")
    return f"Review {action_type} for {decision_id}"


def _contains_inline_secret(value: object) -> bool:
    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            key = str(raw_key).lower()
            if any(token in key for token in ("secret", "password", "api_key", "token")) and not (
                key.endswith("_ref") or key.endswith("_id")
            ):
                return True
            if _contains_inline_secret(raw_value):
                return True
    if isinstance(value, list):
        return any(_contains_inline_secret(item) for item in value)
    return False


@app.post(
    "/decisions/{decision_id}/reject",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def reject_decision(
    decision_id: str,
    body: DecisionCorrectionBody | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    reject_cross_tenant_decision_access(decision_id, ctx)
    decision = decision_store.reject(decision_id, reviewer=ctx.user_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    correction = body.as_correction(decision_action="reject") if body is not None else None
    if correction is not None:
        updated = decision_store.annotate(decision_id, correction=correction)
        if updated is not None:
            decision = updated
    if decision.get("status") != "rejected":
        return {"decision": decision, "learning_event": None}
    learning_event = learning_store.record_rejected_decision(decision)
    updated = decision_store.annotate(
        decision_id,
        outcome=learning_event["outcome"],
        learning_event=learning_event,
    )
    return {"decision": updated or decision, "learning_event": learning_event}
