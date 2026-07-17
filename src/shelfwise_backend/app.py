from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic, sleep
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
    SourceSystem,
    connector_status_for_policy,
    list_connector_capabilities,
    map_for,
    record_to_event,
)
from shelfwise_contracts import Event, EventSource, EventType, Money
from shelfwise_inference import (
    InferenceError,
    OpenAICompatibleInferenceClient,
    ProviderKind,
    load_inference_config,
)
from shelfwise_inference.orchestration import ExecutionMode
from shelfwise_mlops import (
    ModelRun,
    OutcomeRecord,
    SkillStats,
    build_accountability_report,
    decision_economics,
    draft_skills,
)
from shelfwise_mlops import activate as activate_skill
from shelfwise_mlops import to_plan as skill_to_plan
from shelfwise_mlops.skill_registry import discover as discover_skills
from shelfwise_mlops.skill_registry import promote as promote_skill_manifest
from shelfwise_mlops.skill_registry import retire as retire_skill_manifest
from shelfwise_runtime.provenance import DataDomain, DataDomainBoundaryError
from shelfwise_storage import (
    TENANT_SCOPED_TABLES,
    bind_tenant_context,
    default_tenant_profile,
    reset_tenant_context,
)
from shelfwise_worldgen.scenarios import build as build_worldgen_scenario

from .agentic_cascade import (
    AgenticCascadeDeadlineError,
    AgenticCascadeError,
    run_catalog_price_check_via_agents,
    run_cold_chain_cascade_via_agents,
    run_expiry_risk_check_via_agents,
    run_golden_cascade_via_agents,
    run_procurement_cascade_via_agents,
    run_sales_cascade_via_agents,
)
from .cascade import (
    run_cold_chain_cascade,
    run_critic_rejection_cascade,
    run_golden_cascade,
    run_procurement_cascade,
    run_sales_cascade,
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
from .context_budget import build_context_receipt
from .conversation_memory import compact_conversation
from .conversation_routing import ConversationRouteRequest, choose_conversation_route
from .deps import (
    _COOKIE_OVERRIDE_ENV,
    _INSECURE_APP_ENV_NAMES,
    _TRUE_ENV_VALUES,
    APPROVAL_AUTH_DEP,
    CURRENT_TENANT_DEP,
    INGEST_AUTH_DEP,
    OWNER_AUTH_DEP,
    SESSION_COOKIE,
    WORKER_AUTH_DEP,
    WRITE_LIMIT_DEP,
    _auth_mode,
    _cookie_secure_setting,
    _env_positive_int,
    _is_production_deployment,
    _request_authorization,
    _tenant_id_from_request,
    worker_internal_guard,
    write_limiter,  # noqa: F401  (re-exported: tests/conftest.py imports it from here)
    write_path_guard,
)
from .detective import analyze_root_cause, root_cause_cte_sql
from .intelligence_api import router as intelligence_router
from .observability import build_observability_snapshot
from .operational_facts import MissingOperationalFacts
from .product_catalog import product_attention_queue, search_product_catalog
from .routes_twin import router as twin_router
from .state import (
    candidate_store,
    cascade_dispatcher,
    cascade_worker,
    chat_store,
    cold_chain_feed,
    connector_cursor_store,
    conversation_memory_store,
    decision_store,
    event_bus,
    event_store,
    fidelity_revalidation_service,
    inbound_record_store,
    inventory_position_store,
    journal,
    learning_store,
    model_run_registry,
    open_order_store,
    operational_facts_for_query,
    plan_runner,
    product_catalog_store,
    prompt_registry,
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
from .worker import MemoryConsolidationWorker, worker_enabled

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


def _request_timeout_seconds() -> float:
    """Return the configurable request deadline for real application operation."""
    ceiling = 900.0
    raw = os.getenv("SHELFWISE_REQUEST_TIMEOUT_SECONDS", "120")
    try:
        value = float(raw)
    except ValueError:
        value = 120.0
    return min(max(value, 1.0), ceiling)


def _require_amd_inference() -> None:
    """Reject non-AMD providers in named deployments before any model request."""
    if (
        _is_production_deployment()
        and load_inference_config().provider is not ProviderKind.VLLM_MI300X
    ):
        raise HTTPException(status_code=503, detail="AMD inference is not configured")


def _agentic_unavailable(exc: AgenticCascadeError) -> HTTPException:
    """Log provider diagnostics without exposing endpoint or credential details to clients."""
    _LOGGER.warning("agentic inference unavailable: %s", str(exc)[:500])
    return HTTPException(status_code=503, detail="Live agentic inference is unavailable")


def _agentic_deadline_exceeded(exc: AgenticCascadeDeadlineError) -> HTTPException:
    """Return a structured 503 when a cascade stops itself before the response deadline.

    This is the deliberate alternative to letting the request run past
    `_request_timeout_seconds()` and get killed by `enforce_request_deadline` - the cascade
    reports how far it got instead of leaving the caller with a bare timeout.
    """
    _LOGGER.warning("agentic cascade stopped before its deadline: %s", str(exc)[:500])
    return HTTPException(
        status_code=503,
        detail={
            "detail": "cascade could not finish inside the response deadline",
            "completed_model_calls": exc.completed_model_calls,
            "elapsed_ms": exc.elapsed_ms,
        },
    )


def _cascade_deadline() -> float:
    """Absolute monotonic deadline a cascade must stop calling models before."""
    return monotonic() + _request_timeout_seconds() - 1.0


def _production_execution_mode(requested_live: bool) -> ExecutionMode:
    """Force production agentic routes onto live AMD inference."""
    if _is_production_deployment():
        _require_amd_inference()
        return ExecutionMode.LIVE_REQUIRED
    return ExecutionMode.LIVE_REQUIRED if requested_live else ExecutionMode.OFFLINE_TEST


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


_reject_insecure_auth_in_named_deployments()
_reject_insecure_production_cookie_config()

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

try:
    from shelfwise_multimodal.router import build_scan_router, build_voice_router
except ImportError:
    build_scan_router = None
    build_voice_router = None

if build_voice_router is not None:
    app.include_router(build_voice_router())
if build_scan_router is not None:
    app.include_router(build_scan_router())

app.router.add_event_handler("startup", worker_service.start)
app.router.add_event_handler("shutdown", worker_service.stop)
app.router.add_event_handler("startup", cold_chain_feed.start)
app.router.add_event_handler("shutdown", cold_chain_feed.stop)
app.router.add_event_handler("startup", twin_projection_service.start)
app.router.add_event_handler("shutdown", twin_projection_service.stop)
app.router.add_event_handler("startup", fidelity_revalidation_service.start)
app.router.add_event_handler("shutdown", fidelity_revalidation_service.stop)


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
    configured_email = os.getenv("SHELFWISE_LOGIN_EMAIL", "").strip().lower()
    configured_hash = os.getenv("SHELFWISE_LOGIN_PASSWORD_HASH", "").strip()
    if not secret or not configured_email or not configured_hash:
        raise HTTPException(status_code=503, detail="Company login is not configured")
    if not _login_credentials_valid(
        email=body.email, password=body.password,
        configured_email=configured_email, configured_hash=configured_hash,
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    ctx = TenantContext(
        tenant_id=default_tenant_context().tenant_id,
        user_id=configured_email,
        role=Role.OWNER,
    )
    lifetime = _env_positive_int("SHELFWISE_LOGIN_SESSION_SECONDS", 43_200)
    token = encode_hs256_token(
        {**ctx.to_dict(), "exp": int(datetime.now(UTC).timestamp()) + lifetime},
        secret=secret,
    )
    response = JSONResponse({"session": ctx.to_dict(), "mode": "jwt"})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=lifetime, httponly=True,
        secure=_cookie_secure_setting(), samesite="strict", path="/",
    )
    return response


def _login_credentials_valid(
    *, email: str, password: str, configured_email: str, configured_hash: str
) -> bool:
    """Constant-shape verification: hash first, compare both, no early-exit oracle."""
    import hashlib
    import hmac as _hmac

    try:
        scheme, salt_hex, hash_hex = configured_hash.split("$", 2)
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        computed = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    email_ok = _hmac.compare_digest(email.strip().lower(), configured_email)
    password_ok = _hmac.compare_digest(computed, expected)
    return email_ok and password_ok


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


class ProductUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)


class ProductVariantUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, max_length=200)
    pack_size: str | None = Field(default=None, max_length=100)
    unit_of_measure: str | None = Field(default=None, max_length=50)
    is_case_pack: bool = False


class ProductIdentifierUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, max_length=200)
    kind: str
    value: str = Field(min_length=1, max_length=200)
    source_system: str | None = Field(default=None, max_length=100)


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
            "response_timeout": (
                "configured" if _request_timeout_seconds() > 0 else "missing"
            ),
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
            recorder=lambda payload: _record_model_run(
                {**payload, "data_domain": data_domain}
            )
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

    return _record_pipeline_event(event)


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
        return _record_pipeline_event(Event.parse_wire(existing or {}))

    reviewed_event = replace(
        event,
        payload={
            **event.payload,
            "reviewed_by": ctx.user_id,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "review_note": body.review_note,
        },
    )
    return _record_pipeline_event(reviewed_event)


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
        decisions=_tenant_scoped_decisions(ctx),
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
            {
                key: value
                for key, value in item.items()
                if key != "messages"
            }
            | {"data_domain": _conversation_data_domain(item)}
            | {"message_count": len(item["messages"])}
            for item in conversations
        ]
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
        existing_domains.update({
            str(item.get("metadata", {}).get("data_domain"))
            for item in (conversation or {}).get("messages", [])
            if isinstance(item, dict)
            and isinstance(item.get("metadata"), dict)
            and item.get("metadata", {}).get("data_domain")
        })
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
    decisions = _tenant_scoped_decisions(ctx, data_domain=chat_domain)
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

    # Context receipt (plan Section 41.3): account the conversational sections this
    # request contributes and fail closed on overflow BEFORE any network I/O.
    context_receipt = build_context_receipt(
        sections={
            "question": body.question,
            "recent_turns": json.dumps(state.get("conversation_history", [])),
            "pinned_and_summary": str(state.get("conversation_summary", "")),
            "skill_catalogue": json.dumps(state.get("skill_catalogue", [])),
        },
        selected_memory_ids=(
            (conversation_summary.id,) if conversation_summary is not None else ()
        ),
        selected_skill_ids=tuple(manifest.id for manifest in discovered_skills),
        selected_tools=tuple(
            tool for manifest in discovered_skills for tool in manifest.required_tools
        ),
        truncated=bool(conversation_summary is not None),
    )
    client = OpenAICompatibleInferenceClient(
        recorder=lambda payload: _record_model_run(
            {**payload, "data_domain": chat_domain}
        )
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
        )
    except InferenceError as exc:
        raise HTTPException(status_code=503, detail="Live chat inference failed") from exc
    _meta["correlation_id"] = correlation_id
    _meta["data_domain"] = chat_domain
    _meta["conversation_route"] = conversation_route.to_dict()
    _meta["context_receipt"] = context_receipt.to_dict()
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


@app.get("/scenarios/worldgen-runs")
def list_worldgen_runs(
    limit: int = 100,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        runs = worldgen_run_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"runs": runs}


@app.get("/scenarios/worldgen-runs/{run_id}")
def get_worldgen_run(
    run_id: str, ctx: TenantContext = CURRENT_TENANT_DEP
) -> dict[str, object]:
    run = worldgen_run_store.get(run_id, tenant_id=ctx.tenant_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Worldgen run not found")
    return {"run": run}


@app.get("/mlops/model-runs")
def list_model_runs(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    resolved_domain = data_domain or _chat_data_domain()
    runs = model_run_registry.list(
        tenant_id=tenant_id,
        data_domain=resolved_domain,
    )
    return {
        "data_domain": resolved_domain,
        "model_runs": [run.to_dict() for run in runs],
    }


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


@app.get("/connectors/systems")
def list_connector_systems() -> dict[str, object]:
    return {"systems": [item.to_dict() for item in list_connector_capabilities()]}


@app.get("/connectors/me")
def list_tenant_connectors(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    profile = tenant_profile_store.get(ctx.tenant_id) or default_tenant_profile(ctx.tenant_id)
    policy = (
        profile.get("connector_policy") if isinstance(profile.get("connector_policy"), dict) else {}
    )
    return {
        "tenant_id": ctx.tenant_id,
        "connector_policy": policy,
        "systems": connector_status_for_policy(policy),
    }


@app.get("/connectors/inbound-records")
def list_inbound_records(
    limit: int = 200,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        records = inbound_record_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"tenant_id": ctx.tenant_id, "records": records}


@app.post("/catalog/products", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def upsert_product(
    body: ProductUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    product = Product(
        tenant_id=ctx.tenant_id,
        product_id=body.product_id,
        name=body.name,
        category=body.category,
        brand=body.brand,
    )
    return {"product": product_catalog_store.upsert_product(product)}


@app.get("/catalog/products")
def list_products(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return {"products": product_catalog_store.list_products(tenant_id=ctx.tenant_id)}


@app.get("/catalog/products/{product_id}")
def get_product(
    product_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    product = product_catalog_store.get_product(tenant_id=ctx.tenant_id, product_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"product": product}


@app.post(
    "/catalog/products/{product_id}/variants",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_product_variant(
    product_id: str,
    body: ProductVariantUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    variant = ProductVariant(
        tenant_id=ctx.tenant_id,
        variant_id=body.variant_id,
        product_id=product_id,
        pack_size=body.pack_size,
        unit_of_measure=body.unit_of_measure,
        is_case_pack=body.is_case_pack,
    )
    return {"variant": product_catalog_store.upsert_variant(variant)}


@app.get("/catalog/products/{product_id}/variants")
def list_product_variants(
    product_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    variants = product_catalog_store.list_variants(tenant_id=ctx.tenant_id, product_id=product_id)
    return {"variants": variants}


@app.post(
    "/catalog/identifiers",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_product_identifier(
    body: ProductIdentifierUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    try:
        identifier = ProductIdentifier(
            tenant_id=ctx.tenant_id,
            variant_id=body.variant_id,
            kind=body.kind,
            value=body.value,
            source_system=body.source_system,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return {"identifier": product_catalog_store.upsert_identifier(identifier)}
    except ConflictingIdentifierError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/catalog/resolve")
def resolve_product_identifier(
    kind: str,
    value: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Resolve a single source-system code (GTIN/barcode/SKU/PLU/source id) to a variant.

    This is the identity-resolution seam every downstream reasoning path (expiry,
    reorder, demand) needs before it can safely trust "which physical item is this".
    """
    variant = product_catalog_store.resolve_identifier(
        tenant_id=ctx.tenant_id, kind=kind, value=value
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="No variant resolves to that identifier")
    return {"variant": variant}


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

    pipeline = _record_pipeline_event(event)
    return {
        "status": pipeline["status"],
        "record": stored_record,
        "event": pipeline["event"],
        "pipeline": pipeline,
    }


connector_poll_service = ConnectorPollService(
    cursors=connector_cursor_store,
    process_record=_process_inbound_record,
    tenant_id=default_tenant_context().tenant_id,
)
app.router.add_event_handler("startup", connector_poll_service.start)
app.router.add_event_handler("shutdown", connector_poll_service.stop)


@app.get("/connectors/poll/status")
def connector_poll_status() -> dict[str, object]:
    """Report the background ERP/WMS poll loop's configured systems and last run."""
    return connector_poll_service.status()


@app.get("/mlops/prompts")
def list_prompt_versions(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    prompts = prompt_registry.list(tenant_id=tenant_id)
    return {"prompt_versions": [prompt.to_dict() for prompt in prompts]}


@app.get("/mlops/accountability")
def accountability_report(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    # Derive the tenant from the authenticated context, never a caller-supplied query
    # param - accepting an arbitrary tenant_id here let any authenticated caller read
    # another tenant's model-run and decision accountability data.
    resolved_domain = data_domain or _chat_data_domain()
    runs = model_run_registry.list(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
    )
    report = build_accountability_report(
        tenant_id=ctx.tenant_id,
        decisions=_tenant_scoped_decisions(ctx, data_domain=resolved_domain),
        models_used=[run.model for run in runs],
        prompt_versions=[run.prompt_version for run in runs],
    )
    return {
        "data_domain": resolved_domain,
        "report": report.to_dict(),
        "markdown": report.to_markdown(),
    }


@app.get("/mlops/observability")
def observability_snapshot(
    limit: int = 500,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    resolved_domain = data_domain or _chat_data_domain()
    try:
        events = event_store.list(
            limit=limit,
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        )
        inbound_records = (
            inbound_record_store.list(tenant_id=ctx.tenant_id, limit=limit)
            if resolved_domain == DataDomain.OPERATIONAL_TWIN.value
            else []
        )
        candidate_records = candidate_store.list(
            ctx.tenant_id,
            data_domain=resolved_domain,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = build_observability_snapshot(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
        decisions=_tenant_scoped_decisions(ctx, data_domain=resolved_domain),
        model_runs=[
            run.to_dict()
            for run in model_run_registry.list(
                tenant_id=ctx.tenant_id,
                data_domain=resolved_domain,
            )
        ],
        inbound_records=inbound_records,
        events=events,
        bus_stats={
            **event_bus.stats(tenant_id=ctx.tenant_id),
            "scope": "tenant_all_domains",
        },
        writeback_tasks=writeback_sink.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        worker_status=worker_service.status(),
        worker_runs=journal.list_runs(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        learning_events=learning_store.list_events(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
        ),
        tenant_facts=tenant_fact_store.list(
            tenant_id=ctx.tenant_id,
            data_domain=resolved_domain,
            active_only=False,
        ),
        rate_zar_per_1k=_inference_rate(),
        candidate_records=candidate_records,
        open_orders=open_order_store.list(
            ctx.tenant_id,
            data_domain=resolved_domain,
            limit=limit,
        ),
    )
    return {"data_domain": resolved_domain, "snapshot": snapshot}


@app.get("/mlops/tenant-facts")
def list_tenant_facts(
    include_tombstoned: bool = False,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    facts = tenant_fact_store.list(
        tenant_id=ctx.tenant_id,
        data_domain=resolved_domain,
        active_only=not include_tombstoned,
    )
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": resolved_domain,
        "facts": facts,
    }


@app.post(
    "/mlops/consolidate-memory",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def consolidate_memory(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = WORKER_AUTH_DEP,
) -> dict[str, object]:
    return _memory_consolidation_worker().process_tenant(
        ctx.tenant_id,
        data_domain=data_domain or _chat_data_domain(),
    )


# Plan-step templates for playbooks mined from outcome history. Capabilities name the
# same governed action types the HITL/writeback path already uses; a compiled plan is a
# governed RECOMMENDATION artifact (like a writeback task), never an autonomous write -
# execution stays behind the capability registry and human approval.
_MINED_SKILL_STEP_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "apply_markdown": [
        {
            "key": "apply_markdown",
            "capability": "apply_markdown",
            "params": {},
            "compensation": {"undo": "restore_catalog_price"},
        }
    ],
    "reorder": [
        {
            "key": "reorder",
            "capability": "reorder",
            "params": {},
            "compensation": {"undo": "cancel_pending_purchase_order"},
        }
    ],
    "quarantine_stock": [
        {
            "key": "quarantine_stock",
            "capability": "quarantine_stock",
            "params": {},
            "compensation": {"undo": "release_quarantine_hold"},
        }
    ],
    "dispatch_facilities_check": [
        {
            "key": "dispatch_facilities_check",
            "capability": "dispatch_facilities_check",
            "params": {},
            "compensation": {"undo": "cancel_facilities_dispatch"},
        }
    ],
}


def _mined_skill_drafts(tenant_id: str, data_domain: str) -> list[Any]:
    """Mine playbook drafts from this tenant's REAL resolved-outcome history.

    Trigger is the decision's scenario id - the same stable workload classification the
    rest of the platform uses - so a draft reads as "apply_markdown when
    stage4_loadshedding_x_payday_yoghurt" and its evidence refs point at the actual
    decisions that earned it.
    """
    decisions = {
        str(decision.get("id")): decision
        for decision in decision_store.list()
        if _decision_tenant_id(decision, tenant_id) == tenant_id
    }
    stats = SkillStats()
    for record in _learning_outcome_records(tenant_id, data_domain):
        decision = decisions.get(record.evidence_refs[0]) if record.evidence_refs else None
        scenario_id = str((decision or {}).get("scenario_id") or "") or record.action
        stats.reflect(record, trigger=scenario_id)
    return draft_skills(stats, tenant_id=tenant_id, step_template=_MINED_SKILL_STEP_TEMPLATES)


@app.get("/mlops/skills/mined")
def list_mined_skills(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Playbooks mined from repeated, measurably successful outcomes - drafts for review."""
    resolved_domain = data_domain or _chat_data_domain()
    drafts = _mined_skill_drafts(ctx.tenant_id, resolved_domain)
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": resolved_domain,
        "skills": [skill.to_dict() for skill in drafts],
    }


@app.post(
    "/mlops/skills/mined/{skill_id}/activate",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def activate_mined_skill(
    skill_id: str,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Activate a reviewed mined draft and compile it to the validated plan shape.

    The compiled plan is returned as a governed artifact for the approving human - it is
    NOT executed here. Activation re-mines from current outcome history, so a draft that
    later outcomes no longer support simply no longer exists to activate (the honest
    tombstone: evidence, not memory of past drafts, decides what is activatable).
    """
    resolved_domain = data_domain or _chat_data_domain()
    draft = next(
        (
            skill
            for skill in _mined_skill_drafts(ctx.tenant_id, resolved_domain)
            if skill.id == skill_id
        ),
        None,
    )
    if draft is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No currently-minable draft has that id - either it never existed or "
                "later outcomes no longer support it"
            ),
        )
    active = activate_skill(draft)
    plan = skill_to_plan(
        active, plan_id=f"plan_{skill_id}_{ctx.tenant_id}", actor_role=ctx.role.value
    )
    return {"skill": active.to_dict(), "plan": plan}


@app.get("/mlops/skills")
def list_skill_manifests(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    """List the assistant skill catalogue this tenant can discover, with lifecycle status."""
    manifests = skill_registry.list(tenant_id=ctx.tenant_id)
    return {
        "tenant_id": ctx.tenant_id,
        "skills": [manifest.to_dict() for manifest in manifests],
    }


class SkillPromotionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured_pass_rate: float = Field(ge=0.0, le=1.0)


@app.post(
    "/mlops/skills/{skill_id}/promote",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def promote_skill(
    skill_id: str,
    body: SkillPromotionBody,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Flip a draft skill to promoted - only past its own evaluation bar.

    The promotion gate is the enforcement point that makes the lifecycle real: discovery
    only ever surfaces promoted manifests, so this route is how a validated draft skill
    actually reaches conversations. Requires an approval-capable role, like every other
    governance write.
    """
    try:
        manifest = promote_skill_manifest(
            skill_registry,
            skill_id,
            measured_pass_rate=body.measured_pass_rate,
            tenant_id=ctx.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": manifest.to_dict()}


@app.post(
    "/mlops/skills/{skill_id}/retire",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def retire_skill(
    skill_id: str,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Retire a skill from discovery permanently (re-register a new version to revive)."""
    try:
        manifest = retire_skill_manifest(skill_registry, skill_id, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": manifest.to_dict()}


@app.post(
    "/mlops/plans/execute",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
async def execute_plan(
    body: dict[str, Any],
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    """Execute a validated governed plan through the journaled runner.

    The governed-write phase, live: the only registered write capability is the HITL
    write-back task sink, every step is journaled with compensation recorded, and the
    plan's tenant is forced to the caller's - a plan can never execute across tenants.
    Compile plans via /mlops/skills/mined/{id}/activate; execute them here after review.
    """
    from shelfwise_backend.worker.plans import Plan as _Plan

    try:
        plan = _Plan.model_validate({**body, "tenant_id": ctx.tenant_id})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:400]) from exc
    result = await plan_runner.run(plan)
    if result.status != "done":
        raise HTTPException(
            status_code=422,
            detail=f"plan failed at step: {result.failed_step}",
        )
    return {"result": result.to_dict()}


@app.get("/worker/schedules")
def list_schedules() -> dict[str, object]:
    """Recurring governed schedules and their receipts (fidelity revalidation today)."""
    return {"fidelity_revalidation": fidelity_revalidation_service.status()}


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
        result["cascade"] = _record_cascade(cascade)
    return {"result": result}


_SCENARIO_WRITE_DEPS = [Depends(write_path_guard), WRITE_LIMIT_DEP]


def _demo_occurrence_suffix(
    key: str, *, id_prefix: str, tenant_id: str, data_domain: str = "world_simulation"
) -> str:
    """Resolve a stable-but-not-stuck suffix for a demo trigger keyed by (tenant, type, sku, day).

    Repeated clicks against a still-pending decision for this key must reuse its id (upsert in
    place) so the approval queue does not grow one identical duplicate card per click. But once
    that decision is resolved (approved/rejected), a further trigger for the same key is a
    genuinely new occurrence and must get a new id, not resurrect the resolved one. We walk an
    occurrence counter and stop at the first slot that is either free or still pending.
    """
    occurrence = 0
    while True:
        suffix = hashlib.sha256(f"{key}:{occurrence}".encode()).hexdigest()[:12]
        decision_id = f"dec_{_slug_tenant(tenant_id)}_{data_domain}_{id_prefix}_{suffix}"
        existing = decision_store.get(decision_id)
        if existing is None or (existing.get("status") or "pending").lower() == "pending":
            return suffix
        occurrence += 1


def _slug_tenant(value: str) -> str:
    """Keep demo lookup IDs aligned with cascade replay IDs."""
    clean = "".join(char if char.isalnum() else "_" for char in value.strip().lower())
    return clean.strip("_") or "local"


def _demo_run_scope(value: str | None) -> str:
    """Return a bounded key suffix for an explicitly scoped automated demo run."""
    if not value or not value.strip():
        return ""
    digest = hashlib.sha256(value.strip().encode()).hexdigest()[:12]
    return f":run:{digest}"


def _demo_occurrence_ts(today: date) -> datetime:
    """Return a deterministic timestamp for a day-scoped, idempotent demo trigger.

    `id`/`correlation_id` for these events are already deterministic per (tenant, type,
    sku, day) so a resubmission of the same logical trigger reuses the same id - that is
    the documented idempotency contract (`_demo_occurrence_suffix`). But `Event.ts` was
    generated fresh via `datetime.now(UTC)` on every call, so `_same_event_payload` saw a
    different timestamp on any resubmission more than an instant apart and rejected it with
    409 "different content" - a legitimate repeat click or webhook retry, not a real
    conflict. Pinning `ts` to midnight UTC of the same day keeps it inside the existing
    day-bucket the key is already scoped to, so a real resubmission is byte-identical.
    """
    return datetime(today.year, today.month, today.day, tzinfo=UTC)


def _demo_event(
    ctx: TenantContext, event_type: EventType, *, variant: str = "deterministic"
) -> Event:
    """Create a tenant-owned trigger from the tenant's generated world facts.

    The id is derived deterministically from (tenant, event type, sku, day) rather than a
    random uuid - see `_demo_occurrence_suffix` for why repeated clicks dedupe while a new
    occurrence after resolution still gets a fresh id.
    """
    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    supplier = world_facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today = datetime.now(UTC).date().isoformat()
    variant_slug = _slug_tenant(variant)
    legacy = variant_slug == "deterministic"
    scope = "" if legacy else f":{variant_slug}"
    prefix_scope = "" if legacy else f"_{variant_slug}"
    key = f"{ctx.tenant_id}{scope}:{event_type.value}:{scenario.sku}:{today}"
    id_prefix = f"evt_demo{prefix_scope}_{event_type.value}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix=id_prefix, tenant_id=ctx.tenant_id
    )
    return Event(
        id=f"{id_prefix}_{suffix}",
        type=event_type,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=(
            f"demo_{event_type.value}_{suffix}"
            if legacy
            else f"demo_{variant_slug}_{event_type.value}_{suffix}"
        ),
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "supplier": str(supplier["name"]).lower(),
            "site_id": scenario.location,
        },
    )


def _reject_operational_domain_for_synthetic_drill(
    data_domain: str | None, *, drill: str
) -> None:
    """Fail closed when a synthetic-anomaly drill is pointed at real twin data."""
    if data_domain == DataDomain.OPERATIONAL_TWIN.value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The {drill} drill fabricates a synthetic anomaly and is "
                "simulation-only; live operational anomalies enter through the real "
                "ingest pipeline, never through a drill projected onto twin data."
            ),
        )


def _agentic_cascade_context(
    ctx: TenantContext,
    event_type: EventType,
    *,
    data_domain: Literal["operational_twin", "world_simulation"] | None,
    store_id: str | None,
) -> tuple[Any, Event]:
    """Resolve the (facts, trigger event) pair an agentic demo route reasons over.

    World-simulation stays the existing `_demo_event`/`world_facts` path unchanged. Operational
    reads the same reported twin state the Critic/Executive tools already know how to consume
    (`OperationalFactsProvider` implements the same `get_scenario_facts`/`get_supplier_for_sku`
    contract `WorldFactsProvider` does), so no cascade math or tool code needs to branch on
    domain - only which facts object is handed to it.
    """
    resolved_domain = data_domain or _chat_data_domain()
    if resolved_domain == DataDomain.WORLD_SIMULATION.value:
        return world_facts, _demo_event(ctx, event_type, variant="agentic")

    facts = operational_facts_for_query(ctx.tenant_id, store_id=store_id)
    scenario = facts.get_scenario_facts(ctx.tenant_id)
    supplier = facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:{event_type.value}:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key,
        id_prefix=f"evt_operational_{event_type.value}",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.OPERATIONAL_TWIN.value,
    )
    event = Event(
        id=f"evt_operational_{event_type.value}_{suffix}",
        type=event_type,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.OPERATIONAL_TWIN,
        correlation_id=f"operational_{event_type.value}_{suffix}",
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "store_id": store_id or scenario.location,
            "supplier": str(supplier["name"]).lower(),
            "site_id": scenario.location,
        },
    )
    return facts, event


def _demo_catalog_price_event(ctx: TenantContext) -> Event:
    """Create a generated-world POS price exception for the agentic guardrail route."""
    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:catalog_price_agentic:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_catalog_price_agentic", tenant_id=ctx.tenant_id
    )
    observed = scenario.unit_price * Decimal("1.20")
    return Event(
        id=f"evt_demo_catalog_price_agentic_{suffix}",
        type=EventType.SALE,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_catalog_price_agentic_{suffix}",
        payload={
            "sku": scenario.sku,
            "location": scenario.location,
            "units": 2,
            "unit_price_cents": observed.minor_units,
            "catalog_price_cents": scenario.unit_price.minor_units,
        },
    )


def _demo_expiry_risk_event(ctx: TenantContext) -> Event:
    """Create a generated-world imminent-expiry event for the agentic guardrail route."""
    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today = datetime.now(UTC).date().isoformat()
    key = f"{ctx.tenant_id}:expiry_risk_agentic:{scenario.sku}:{today}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_expiry_risk_agentic", tenant_id=ctx.tenant_id
    )
    return Event(
        id=f"evt_demo_expiry_risk_agentic_{suffix}",
        type=EventType.EXPIRY_ENTRY,
        ts=datetime.now(UTC),
        actor=ctx.user_id,
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_expiry_risk_agentic_{suffix}",
        payload={
            "sku": scenario.sku,
            "batch_id": f"BATCH-{scenario.sku}",
            "category": scenario.category,
            "location": scenario.location,
            "days_to_expiry": 1,
        },
    )


def _preview_demo_cascade(result: dict[str, Any]) -> dict[str, Any]:
    """Enrich a read-only demo preview without mutating stores or traces."""
    _attach_decision_governance(result)
    return result


def _assign_result_tenant(result: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["tenant_id"] = tenant_id
    result["tenant_id"] = tenant_id
    return result


@app.post("/scenarios/golden", dependencies=_SCENARIO_WRITE_DEPS)
def demo_golden(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return _record_cascade(
        run_golden_cascade(_demo_event(ctx, EventType.SCAN), facts=world_facts)
    )


@app.post("/scenarios/recall", dependencies=_SCENARIO_WRITE_DEPS)
def demo_recall(
    run_scope: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Drive a generated-world supplier recall through the real event and HITL pipeline."""
    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    supplier = world_facts.get_supplier_for_sku(ctx.tenant_id, scenario.sku)
    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    key = f"{ctx.tenant_id}:recall:{scenario.sku}:{today}{_demo_run_scope(run_scope)}"
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_recall", tenant_id=ctx.tenant_id
    )
    event = Event(
        id=f"evt_demo_recall_{suffix}",
        type=EventType.RECALL_NOTICE,
        ts=_demo_occurrence_ts(today_date),
        actor=f"supplier_{supplier['supplier_id']}",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_recall_{suffix}",
        payload={
            "recall_id": f"REC-DEMO-{suffix}",
            "sku": scenario.sku,
            "lot_id": f"LOT-{scenario.sku}",
            "units": max(1, scenario.units_on_hand // 4),
            "location": scenario.location,
            "reason": "possible cold-chain contamination",
            "issued_by": f"{supplier['name']} Quality",
            "issuer_verified": True,
        },
    )
    outcome = _record_pipeline_event(event)
    return _resolve_demo_pipeline_cascade(outcome, event, ctx)


@app.post("/scenarios/inventory-exception", dependencies=_SCENARIO_WRITE_DEPS)
def demo_inventory_exception(
    run_scope: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Drive a generated-world shrink count through the real event and HITL pipeline."""
    scenario = world_facts.get_scenario_facts(ctx.tenant_id)
    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    key = (
        f"{ctx.tenant_id}:inventory_exception:{scenario.sku}:{today}"
        f"{_demo_run_scope(run_scope)}"
    )
    suffix = _demo_occurrence_suffix(
        key, id_prefix="evt_demo_inventory_exception", tenant_id=ctx.tenant_id
    )
    counted_units = max(0, scenario.units_on_hand - max(1, scenario.units_on_hand // 10))
    event = Event(
        id=f"evt_demo_inventory_exception_{suffix}",
        type=EventType.INVENTORY_EXCEPTION,
        ts=_demo_occurrence_ts(today_date),
        actor="cycle_count_team",
        tenant_id=ctx.tenant_id,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"demo_inventory_exception_{suffix}",
        payload={
            "exception_id": f"EXC-DEMO-{suffix}",
            "exception_type": "shrink",
            "sku": scenario.sku,
            "reason": "cycle count below system stock",
            "location": scenario.location,
            "expected_units": scenario.units_on_hand,
            "counted_units": counted_units,
            "count_reference": f"COUNT-{suffix}",
        },
    )
    outcome = _record_pipeline_event(event)
    return _resolve_demo_pipeline_cascade(outcome, event, ctx)


@app.get("/scenarios/golden", dependencies=_SCENARIO_WRITE_DEPS)
def demo_golden_get() -> dict[str, object]:
    return _preview_demo_cascade(run_golden_cascade(facts=world_facts))


@app.post("/scenarios/golden/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_golden_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the golden scenario's Critic/Executive verdicts through a real Gemma tool loop.

    Unlike /scenarios/golden (deterministic math + hand-authored evidence), this route requires
    an actual model call and tool-calling round trip. With live_required=true (default) it
    hard-fails with 503 instead of silently falling back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the Critic/Executive tool calls in reported twin
    state (via `OperationalFactsProvider`) instead of the generated world - the same real
    facts contract `product_attention`/`product_search` already use, applied to the agentic
    tool-calling path for the first time. Requires onboarded twin data for this tenant/store;
    raises 422 if the twin cannot yet answer (see `MissingOperationalFacts`).
    """
    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SCAN, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_golden_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    return _record_cascade(result)


@app.post("/scenarios/procurement/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_procurement_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the procurement reorder/supplier verdicts through a real Gemma tool loop.

    Unlike /scenarios/procurement (deterministic math + hand-authored evidence), this route
    requires an actual model call and tool-calling round trip over get_reorder_policy and
    get_supplier_ranking. With live_required=true (default) it hard-fails with 503 instead
    of silently falling back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state
    (`OperationalFactsProvider`), the same contract the golden agentic route uses;
    raises 422 when the twin cannot yet answer for this tenant/store.
    """
    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SUPPLIER_UPDATE, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_procurement_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    return _record_cascade(result)


@app.post("/scenarios/sales/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_sales_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the POS price-integrity verdict through a real Gemma tool loop.

    Unlike /scenarios/sales (deterministic math + hand-authored evidence), this route requires
    an actual model call and tool-calling round trip over check_price_integrity. With
    live_required=true (default) it hard-fails with 503 instead of silently falling back
    to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state; raises
    422 when the twin cannot yet answer for this tenant/store.
    """
    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.SALE, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_sales_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    return _record_cascade(result)


@app.post("/scenarios/catalog-price/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_catalog_price_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the POS catalogue-price guardrail through a real Gemma tool loop.

    Simulation-only by contract: this drill fabricates a synthetic price outlier to
    demonstrate the guardrail, and fabricating an anomaly from a real store's twin data
    would be invented telemetry. Live operational price exceptions enter through the
    real POS/ingest pipeline, where the catalog-price dispatcher already screens every
    sale.
    """
    _reject_operational_domain_for_synthetic_drill(data_domain, drill="catalog-price")
    mode = _production_execution_mode(live_required)
    try:
        result = run_catalog_price_check_via_agents(
            _demo_catalog_price_event(ctx),
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=world_facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    if result is None:
        raise HTTPException(status_code=500, detail="Catalog-price demo did not produce a decision")
    return _record_cascade(result)


@app.post("/scenarios/expiry-risk/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_expiry_risk_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the imminent-expiry guardrail through a real Gemma tool loop.

    Simulation-only by contract, for the same reason as the catalog-price drill: the
    synthetic near-expiry entry it fabricates must never be projected onto real twin
    data. Live expiry entries arrive through the real ingest pipeline's expiry-risk
    dispatcher.
    """
    _reject_operational_domain_for_synthetic_drill(data_domain, drill="expiry-risk")
    mode = _production_execution_mode(live_required)
    try:
        result = run_expiry_risk_check_via_agents(
            _demo_expiry_risk_event(ctx),
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=world_facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    if result is None:
        raise HTTPException(status_code=500, detail="Expiry-risk demo did not produce a decision")
    return _record_cascade(result)


@app.post("/scenarios/cold-chain/agentic", dependencies=_SCENARIO_WRITE_DEPS)
def demo_cold_chain_agentic(
    live_required: bool = True,
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    store_id: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Run the cold-chain facilities-escalation verdict through a real Gemma tool loop.

    Unlike /scenarios/cold-chain (deterministic math + hand-authored evidence), this route
    requires an actual model call and tool-calling round trip over get_cold_chain_status.
    With live_required=true (default) it hard-fails with 503 instead of silently falling
    back to an offline/deterministic answer.

    `data_domain=operational_twin` grounds the tool calls in reported twin state; raises
    422 when the twin cannot yet answer for this tenant/store.
    """
    mode = _production_execution_mode(live_required)
    try:
        facts, event = _agentic_cascade_context(
            ctx, EventType.COLD_CHAIN_ALERT, data_domain=data_domain, store_id=store_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = run_cold_chain_cascade_via_agents(
            event,
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
            facts=facts,
            audit=tool_audit,
            model_run_recorder=_record_model_run,
            deadline=_cascade_deadline(),
        )
    except AgenticCascadeDeadlineError as exc:
        raise _agentic_deadline_exceeded(exc) from exc
    except AgenticCascadeError as exc:
        raise _agentic_unavailable(exc) from exc
    return _record_cascade(result)


@app.post("/scenarios/critic-rejection", dependencies=_SCENARIO_WRITE_DEPS)
def demo_critic_rejection(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return _record_cascade(
        _assign_result_tenant(run_critic_rejection_cascade(facts=world_facts), ctx.tenant_id)
    )


@app.get("/scenarios/critic-rejection", dependencies=_SCENARIO_WRITE_DEPS)
def demo_critic_rejection_get() -> dict[str, object]:
    return _preview_demo_cascade(run_critic_rejection_cascade(facts=world_facts))


@app.post("/scenarios/procurement", dependencies=_SCENARIO_WRITE_DEPS)
def demo_procurement(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return _record_cascade(
        run_procurement_cascade(_demo_event(ctx, EventType.SUPPLIER_UPDATE), facts=world_facts)
    )


@app.get("/scenarios/procurement", dependencies=_SCENARIO_WRITE_DEPS)
def demo_procurement_get() -> dict[str, object]:
    return _preview_demo_cascade(run_procurement_cascade(facts=world_facts))


@app.post("/scenarios/sales", dependencies=_SCENARIO_WRITE_DEPS)
def demo_sales(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return _record_cascade(
        run_sales_cascade(_demo_event(ctx, EventType.SALE), facts=world_facts)
    )


@app.get("/scenarios/sales", dependencies=_SCENARIO_WRITE_DEPS)
def demo_sales_get() -> dict[str, object]:
    return _preview_demo_cascade(run_sales_cascade(facts=world_facts))


@app.post("/scenarios/cold-chain", dependencies=_SCENARIO_WRITE_DEPS)
def demo_cold_chain(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return _record_cascade(
        run_cold_chain_cascade(_demo_event(ctx, EventType.COLD_CHAIN_ALERT), facts=world_facts)
    )


@app.get("/scenarios/cold-chain", dependencies=_SCENARIO_WRITE_DEPS)
def demo_cold_chain_get() -> dict[str, object]:
    return _preview_demo_cascade(run_cold_chain_cascade(facts=world_facts))


@app.get("/scenarios/worldgen/{scenario_id}", dependencies=_SCENARIO_WRITE_DEPS)
def demo_worldgen_drill(
    scenario_id: str,
    limit: int = 80,
    seed_override: int | None = None,
    assortment_size: int | None = None,
    catalog_scale: str = "supermarket",
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    if assortment_size is not None and not (0 < assortment_size <= 20_000):
        raise HTTPException(status_code=422, detail="assortment_size must be between 1 and 20000")
    try:
        world, schedule = build_worldgen_scenario(
            scenario_id,
            seed_override=seed_override,
            assortment_size=assortment_size,
            catalog_scale=catalog_scale,
            tenant_id=ctx.tenant_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scenario not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run_id = f"worldrun_{uuid4().hex[:12]}"
    records: list[dict[str, object]] = []
    cascades: list[dict[str, Any]] = []
    stream = list(world.run())
    for event in _stride_sample(stream, limit):
        outcome = _record_pipeline_event(event)
        records.append(_pipeline_summary(outcome))
        cascade = outcome.get("cascade")
        if isinstance(cascade, dict):
            cascades.append(cascade)

    alert = _worldgen_cold_chain_alert(
        scenario_id=scenario_id,
        seed=world.cfg.seed,
        tenant_id=world.cfg.tenant_id,
        actor=world.cfg.store_id,
        area=world.cfg.area,
        schedule=schedule,
    )
    alert_outcome = _record_pipeline_event(alert)
    records.append(_pipeline_summary(alert_outcome))
    alert_cascade = alert_outcome.get("cascade")
    if isinstance(alert_cascade, dict):
        cascades.append(alert_cascade)

    decisions = [
        cascade["decision"] for cascade in cascades if isinstance(cascade.get("decision"), dict)
    ]
    run = worldgen_run_store.record(
        {
            "run_id": run_id,
            "tenant_id": world.cfg.tenant_id,
            "scenario_id": scenario_id,
            "seed": world.cfg.seed,
            "status": "completed",
            "synthetic": True,
            "worker_enabled": worker_enabled(),
            "events_total": len(records),
            "events_accepted": sum(1 for item in records if item["status"] == "accepted"),
            "duplicates": sum(1 for item in records if item["status"] == "duplicate"),
            "decisions_total": len(decisions),
            "pending_total": sum(1 for item in decisions if item.get("status") == "pending"),
            "event_ids": [str(item["id"]) for item in records],
            "decision_ids": [str(decision["id"]) for decision in decisions],
            "cascade_scenarios": [
                str(cascade.get("scenario"))
                for cascade in cascades
                if cascade.get("scenario") is not None
            ],
            "schedule_sample": schedule[:5],
        }
    )
    return {
        "run": run,
        "scenario_id": scenario_id,
        "synthetic": True,
        "worker_enabled": worker_enabled(),
        "stream_events_total": len(stream),
        "events_total": len(records),
        "events_accepted": sum(1 for item in records if item["status"] == "accepted"),
        "duplicates": sum(1 for item in records if item["status"] == "duplicate"),
        "schedule_sample": schedule[:5],
        "events": records,
        "cascades": cascades,
        "decisions": decisions,
    }


@app.get("/decisions")
def list_decisions(
    data_domain: Literal["operational_twin", "world_simulation"] | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    resolved_domain = data_domain or _chat_data_domain()
    return {
        "data_domain": resolved_domain,
        "decisions": _tenant_scoped_decisions(ctx, data_domain=resolved_domain),
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
        task.get("completion_receipt")
        if isinstance(task.get("completion_receipt"), dict)
        else {}
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
    if decision is None or _decision_belongs_to_other_tenant(decision, ctx):
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


def _chat_data_domain() -> str:
    """Choose live twin grounding in production while preserving local demo defaults."""
    configured = os.getenv("SHELFWISE_CHAT_DATA_DOMAIN", "").strip().lower()
    if configured:
        if configured not in {"operational_twin", "world_simulation"}:
            raise RuntimeError(
                "SHELFWISE_CHAT_DATA_DOMAIN must be operational_twin or world_simulation"
            )
        return configured
    return "operational_twin" if _is_production_deployment() else "world_simulation"


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
        compact["action"] = {
            key: action[key]
            for key in ("type", "risk_tier")
            if key in action
        }
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


def _tenant_scoped_decisions(
    ctx: TenantContext, *, data_domain: str | None = None
) -> list[dict[str, Any]]:
    """Filter decisions to the authenticated tenant when auth is actually enforced.

    In "off" mode there is exactly one (default) tenant context for the whole process,
    so filtering would be a no-op - skipped there to avoid touching the many callers
    exercised by the default local/test/demo configuration.
    """
    decisions = [
        item
        for item in decision_store.list()
        if data_domain is None
        or str(item.get("data_domain") or "world_simulation") == data_domain
    ]
    if _auth_mode() != "jwt":
        return decisions
    return [item for item in decisions if _decision_tenant_id(item, ctx.tenant_id) == ctx.tenant_id]


def _decision_belongs_to_other_tenant(decision: dict[str, Any], ctx: TenantContext) -> bool:
    if _auth_mode() != "jwt":
        return False
    return _decision_tenant_id(decision, ctx.tenant_id) != ctx.tenant_id


def _reject_cross_tenant_decision_access(decision_id: str, ctx: TenantContext) -> None:
    """404 before any mutation if the decision belongs to a different tenant.

    Checked ahead of the approve/reject store call (not just on read) so a cross-tenant
    approval can never execute even against the in-memory backend, which has no RLS
    backstop. A genuinely-missing decision is left to the normal 404 the store call
    already raises, so this only short-circuits on an actual ownership mismatch.
    """
    if _auth_mode() != "jwt":
        return
    existing = decision_store.get(decision_id)
    if existing is not None and _decision_belongs_to_other_tenant(existing, ctx):
        raise HTTPException(status_code=404, detail="Decision not found")


@app.post(
    "/decisions/{decision_id}/approve",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def approve_decision(
    decision_id: str,
    body: DecisionCorrectionBody | None = None,
    ctx: TenantContext = APPROVAL_AUTH_DEP,
) -> dict[str, object]:
    _reject_cross_tenant_decision_access(decision_id, ctx)
    decision = decision_store.approve(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    if decision.get("status") != "approved":
        return {"decision": decision, "learning_event": None}
    learning_event = learning_store.record_approved_decision(decision)
    write_back = decision.get("write_back") or writeback_sink.create_task(
        idempotency_key=f"writeback:{decision_id}",
        tenant_id=_decision_tenant_id(decision, ctx.tenant_id),
        data_domain=str(decision.get("data_domain") or DataDomain.WORLD_SIMULATION.value),
        title=_writeback_title(decision_id, decision),
        assignee_role=str(decision.get("role") or "manager"),
        action=_decision_action(decision),
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


def _decision_action(decision: dict[str, Any]) -> dict[str, Any]:
    """Return the decision action payload as a task-safe dictionary."""
    action = decision.get("action")
    return action if isinstance(action, dict) else {}


def _decision_tenant_id(decision: dict[str, Any], fallback: str) -> str:
    """Return the persisted decision tenant, falling back to the authenticated tenant."""
    tenant_id = str(decision.get("tenant_id") or "").strip()
    return tenant_id or fallback


def _writeback_title(decision_id: str, decision: dict[str, Any]) -> str:
    """Build a short manager-facing task title from the approved decision."""
    action_type = str(_decision_action(decision).get("type") or "action")
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
    _reject_cross_tenant_decision_access(decision_id, ctx)
    decision = decision_store.reject(decision_id)
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


def _record_pipeline_event(event: Event) -> dict[str, Any]:
    # Recording before publishing (durable-store-first) means a bus failure between the two
    # calls would otherwise orphan the event: stored but never enqueued, with every retry of
    # the same event id short-circuited as a harmless-looking "duplicate". The `published`
    # flag closes that gap - only an event that actually reached the bus is treated as a true
    # duplicate on resubmission; anything recorded-but-unpublished self-heals by publishing
    # on the next attempt with the same id.
    is_new = event_store.record(event)
    if not is_new:
        stored = event_store.get(
            event.id,
            tenant_id=event.tenant_id,
            data_domain=event.data_domain,
        )
        if not _same_event_payload(stored, event):
            raise HTTPException(status_code=409, detail="Event id already has different content")
        if event_store.is_published(
            event.id,
            tenant_id=event.tenant_id,
            data_domain=event.data_domain,
        ):
            return {
                "status": "duplicate",
                "event": event.to_dict(),
                "bus_message_id": None,
                "cascade": None,
                "twin": {"status": "not_replayed"},
            }
    open_order_store.observe_event(event)
    twin_projection = _project_twin_event(event)

    bus_message_id = event_bus.publish(event)
    event_store.mark_published(
        event.id,
        tenant_id=event.tenant_id,
        data_domain=event.data_domain,
    )
    cascade = None if worker_enabled() else _cascade_for_event(event)
    return {
        "status": "accepted",
        "event": event.to_dict(),
        "bus_message_id": bus_message_id,
        "cascade": cascade,
        "twin": twin_projection,
    }


def _scenario_drill_wait_seconds() -> float:
    """How long a scenario drill route waits for the async worker before an honest 503.

    DERIVED from the request budget, not picked: the wait must be long enough to cover
    real queue depth (a fixed 15s guess starves under load) while still answering
    before the deadline middleware's 504 pre-empts the route's own honest 503 - so it
    is the request budget minus a response margin, overridable via
    SHELFWISE_SCENARIO_DRILL_WAIT_SECONDS but always capped below the budget.
    """
    budget = float(_request_timeout_seconds())
    ceiling = max(1.0, budget - 10.0)
    raw = os.getenv("SHELFWISE_SCENARIO_DRILL_WAIT_SECONDS", "").strip()
    try:
        configured = float(raw) if raw else ceiling
    except ValueError:
        configured = ceiling
    # Shorter is always safe here (an early 503 is honest and retryable; no work is
    # stolen) - only the ceiling is load-bearing, so clamp up is not needed.
    return min(max(0.1, configured), ceiling)


_DEMO_DRILL_POLL_S = 0.2  # poll frequency while waiting (how often we look, not a bound)


def _await_worker_cascade(event: Event, ctx: TenantContext) -> dict[str, Any]:
    """Wait for the async worker to actually process a just-published event.

    In the real production topology (`WORKER_ENABLED=true`), a cascade is never computed
    synchronously inline - `_record_pipeline_event` deliberately defers it to the queue
    consumer, same as it would for a real recall or shrink count arriving from a source
    system. A "drill" endpoint that immediately checked `cascade is None` and 500'd was a
    leftover single-process demo assumption; the honest real-app behavior is to wait for
    the worker (it polls every 0.25s) and return the decision it actually produces, or a
    truthful still-processing signal if the wait bound is exceeded - never a fabricated
    failure for a submission that in fact succeeded.
    """
    deadline = monotonic() + _scenario_drill_wait_seconds()
    while monotonic() < deadline:
        for row in decision_store.list():
            caused_by = row.get("caused_by")
            if isinstance(caused_by, (list, tuple)) and event.id in caused_by:
                if str(row.get("tenant_id") or ctx.tenant_id) != ctx.tenant_id:
                    continue
                return _record_cascade({"decision": row})
        sleep(_DEMO_DRILL_POLL_S)
    raise HTTPException(
        status_code=503,
        detail=(
            "event was accepted and queued but the worker has not produced a decision "
            "yet; retry shortly"
        ),
    )


def _resolve_demo_pipeline_cascade(
    outcome: dict[str, Any], event: Event, ctx: TenantContext
) -> dict[str, Any]:
    """Return the real cascade for a pipeline-routed demo event, sync or async."""
    cascade = outcome.get("cascade")
    if isinstance(cascade, dict):
        return cascade
    if outcome.get("status") == "duplicate":
        # A repeat drill click resubmits the same deterministic event id - the original
        # decision already exists (or is still being produced by the worker); look it up
        # instead of treating a legitimate idempotent resubmission as a hard failure.
        for row in decision_store.list():
            caused_by = row.get("caused_by")
            if (
                isinstance(caused_by, (list, tuple))
                and event.id in caused_by
                and str(row.get("tenant_id") or ctx.tenant_id) == ctx.tenant_id
            ):
                return _record_cascade({"decision": row})
    return _await_worker_cascade(event, ctx)


def _same_event_payload(stored: dict[str, Any] | None, event: Event) -> bool:
    if stored is None:
        return False
    expected = event.to_dict()
    return all(stored.get(key) == value for key, value in expected.items())


def _project_twin_event(event: Event) -> dict[str, Any]:
    """Project a canonical event additively without breaking the existing cascade path."""
    try:
        results = twin_service.project_event(event)
    except DataDomainBoundaryError:
        return {
            "status": "skipped_non_operational",
            "event_id": event.id,
            "data_domain": event.data_domain.value,
            "reason": "non-operational events cannot enter the operational twin",
        }
    except Exception as exc:  # pragma: no cover - exercised by deployment fault drills
        _LOGGER.exception("twin projection failed for event %s: %s", event.id, str(exc)[:200])
        return {"status": "quarantined", "event_id": event.id, "reason": "projection_failed"}
    return {
        "status": "projected" if results else "ignored_no_store",
        "event_id": event.id,
        "observations": [result.to_dict() for result in results],
    }
def _learning_outcome_records(tenant_id: str, data_domain: str) -> list[OutcomeRecord]:
    decisions = {
        str(decision.get("id")): decision
        for decision in decision_store.list()
        if _decision_tenant_id(decision, tenant_id) == tenant_id
        and str(decision.get("data_domain") or DataDomain.WORLD_SIMULATION.value)
        == data_domain
    }
    records: list[OutcomeRecord] = []
    for event in learning_store.list_events(
        tenant_id=tenant_id,
        data_domain=data_domain,
    ):
        decision_id = str(event.get("decision_id") or "")
        decision = decisions.get(decision_id)
        if decision is None:
            continue
        action = _decision_action(decision)
        outcome = event.get("outcome") if isinstance(event.get("outcome"), dict) else {}
        try:
            success_score = Decimal(str(outcome.get("success_score") or "0"))
        except (TypeError, ValueError, InvalidOperation):
            success_score = Decimal("0")
        records.append(
            OutcomeRecord(
                tenant_id=tenant_id,
                sku=str(event.get("sku") or action.get("sku") or "unknown"),
                action=str(action.get("type") or "unknown"),
                success_score=success_score,
                evidence_refs=_memory_evidence_refs(decision_id, decision),
                data_domain=data_domain,
            )
        )
    return records


def _memory_consolidation_worker() -> MemoryConsolidationWorker:
    return MemoryConsolidationWorker(
        journal=journal,
        fact_store=tenant_fact_store,
        records_for_tenant=_learning_outcome_records,
    )


def _memory_evidence_refs(decision_id: str, decision: dict[str, Any]) -> tuple[str, ...]:
    refs = [decision_id]
    caused_by = decision.get("caused_by")
    if isinstance(caused_by, list):
        refs.extend(str(item) for item in caused_by if item)
    return tuple(dict.fromkeys(refs))


def _cascade_for_event(event: Event) -> dict[str, Any] | None:
    result = cascade_dispatcher.run(event)
    return _record_cascade(result) if result is not None else None


def _stride_sample(events: list[Event], limit: int) -> list[Event]:
    """Take an evenly spaced, chronological sample across the WHOLE event stream.

    Taking the first N events instead starves the pipeline: the world emits every
    product's 08:00 stock update before its first sale of the day, so with a large
    assortment the window fills with a single event type and the sales/expiry
    cascades never see one event. Deterministic: same stream + limit, same sample.
    """
    if len(events) <= limit:
        return events
    step = len(events) / limit
    return [events[int(index * step)] for index in range(limit)]


def _pipeline_summary(outcome: dict[str, Any]) -> dict[str, object]:
    event = outcome["event"] if isinstance(outcome.get("event"), dict) else {}
    cascade = outcome.get("cascade")
    decision = cascade.get("decision") if isinstance(cascade, dict) else None
    return {
        "status": outcome.get("status"),
        "bus_message_id": outcome.get("bus_message_id"),
        "id": event.get("id"),
        "type": event.get("type"),
        "ts": event.get("ts"),
        "tenant_id": event.get("tenant_id"),
        "cascade_scenario": cascade.get("scenario") if isinstance(cascade, dict) else None,
        "decision_id": decision.get("id") if isinstance(decision, dict) else None,
    }


def _worldgen_cold_chain_alert(
    *,
    scenario_id: str,
    seed: int,
    tenant_id: str,
    actor: str,
    area: str,
    schedule: list[dict[str, Any]],
) -> Event:
    scenario = world_facts.get_scenario_facts(tenant_id)
    first_window = schedule[0] if schedule else {}
    alert_ts = str(first_window.get("end") or first_window.get("start") or "2026-06-23T10:00:00")
    stage = int(first_window.get("stage") or 4)
    measured_outage_hours = max(Decimal(stage) / Decimal("2"), Decimal("2.5"))
    return Event.parse_wire(
        {
            "id": f"evt_{scenario_id}_{seed}_cold_chain_alert",
            "type": EventType.COLD_CHAIN_ALERT.value,
            "ts": alert_ts,
            "actor": actor,
            "source": "api",
            "tenant_id": tenant_id,
            "data_domain": DataDomain.WORLD_SIMULATION.value,
            "correlation_id": f"worldgen:{scenario_id}:{seed}:cold_chain",
            "payload": {
                "site_id": actor,
                "area": area,
                "asset_id": f"cold-chain:{actor}:{scenario.category}",
                "category": scenario.category,
                "diagnosis": "generator_failed",
                "severity": 2,
                "predicted_minutes_to_unsafe": "18",
                "measured_outage_hours": str(measured_outage_hours),
                "temp_c": "8.2",
                "stock_at_risk": (
                    scenario.unit_price * scenario.units_on_hand
                ).to_dict(),
                "synthetic": True,
            },
        }
    )


def _record_cascade(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("data_domain", DataDomain.WORLD_SIMULATION.value)
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision.setdefault("data_domain", result["data_domain"])
    _attach_decision_governance(result)
    decision = result.get("decision")
    if isinstance(decision, dict) and decision.get("id"):
        result["decision"] = decision_store.upsert(decision)
    trace_registry.put(result)
    return result


def _attach_decision_governance(result: dict[str, Any]) -> None:
    decision = result.get("decision")
    if not isinstance(decision, dict):
        return
    expected = (
        decision.get("expected_outcome")
        if isinstance(decision.get("expected_outcome"), dict)
        else {}
    )
    recovered_cents = int(expected.get("incremental_profit_minor_units") or 0)
    measured_tokens = _measured_model_call_tokens(result)
    economics_method = "estimated_deterministic_cascade_tokens"
    if measured_tokens is not None:
        total_tokens = measured_tokens
        economics_method = "measured_model_call_usage"
    else:
        total_tokens = _estimate_cascade_tokens(result)
    rate = _inference_rate()
    decision["economics"] = decision_economics(
        rand_recovered=Money(minor_units=recovered_cents, currency="ZAR"),
        total_tokens=total_tokens,
        rate_zar_per_1k=rate,
    )
    inference = result.get("inference") if isinstance(result.get("inference"), dict) else {}
    decision["governance"] = {
        "correlation_id": result.get("correlation_id"),
        "schema_version": "v1",
        "prompt_version": "deterministic-cascade:v1",
        "models": {
            "routine": inference.get("routine_model", "offline-routine"),
            "strong": inference.get("strong_model", "offline-strong"),
        },
        "provider": inference.get("provider", "offline"),
        "evidence_count": len(
            result.get("evidence") if isinstance(result.get("evidence"), list) else []
        ),
        "economics_method": economics_method,
    }


def _measured_model_call_tokens(result: dict[str, Any]) -> int | None:
    """Sum real per-call token usage when a result carries genuine model call traces."""
    model_calls = result.get("model_calls")
    if not isinstance(model_calls, list) or not model_calls:
        return None
    total = 0
    for call in model_calls:
        if not isinstance(call, dict):
            return None
        usage = call.get("usage")
        if not isinstance(usage, dict):
            return None
        total += int(usage.get("total_tokens") or 0)
    return max(1, total)


def _estimate_cascade_tokens(result: dict[str, Any]) -> int:
    text = " ".join(
        [
            str(result.get("scenario") or ""),
            str(result.get("decision") or ""),
            str(result.get("evidence") or ""),
            str(result.get("trace") or ""),
        ]
    )
    return max(1, (len(text) + 3) // 4)


def _inference_rate() -> Decimal:
    raw = os.getenv("INFERENCE_ZAR_PER_1K", "0.004")
    try:
        rate = Decimal(raw)
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0.004")
    return max(rate, Decimal("0"))


def _record_model_run(payload: dict[str, Any]) -> None:
    normalized = dict(payload)
    normalized.setdefault("data_domain", DataDomain.WORLD_SIMULATION.value)
    model_run_registry.record(ModelRun(**normalized))
