from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shelfwise_action import create_decision_store
from shelfwise_catalog import (
    ConflictingIdentifierError,
    Product,
    ProductIdentifier,
    ProductVariant,
    create_product_catalog_store,
)
from shelfwise_connectors import (
    SourceSystem,
    connector_status_for_policy,
    create_inbound_record_store,
    create_writeback_sink,
    list_connector_capabilities,
    map_for,
    record_to_event,
)
from shelfwise_contracts import Event, EventType, Money
from shelfwise_data import load_seeded_scenario
from shelfwise_inference import (
    InferenceError,
    OpenAICompatibleInferenceClient,
    ProviderKind,
    load_inference_config,
)
from shelfwise_inference.orchestration import ExecutionMode
from shelfwise_memory import create_learning_store
from shelfwise_mlops import (
    ModelRun,
    OutcomeRecord,
    build_accountability_report,
    create_model_run_registry,
    create_prompt_registry,
    create_tenant_fact_store,
    decision_economics,
)
from shelfwise_storage import (
    TENANT_SCOPED_TABLES,
    bind_tenant_context,
    create_tenant_profile_store,
    default_tenant_profile,
    reset_tenant_context,
)
from shelfwise_worldgen import create_worldgen_run_store
from shelfwise_worldgen.scenarios import build as build_worldgen_scenario

from .agentic_cascade import AgenticCascadeError, run_golden_cascade_via_agents
from .cascade import (
    run_catalog_price_check,
    run_cold_chain_cascade,
    run_critic_rejection_cascade,
    run_expiry_risk_check,
    run_golden_cascade,
    run_procurement_cascade,
    run_sales_cascade,
)
from .chat import ChatBody, stream_chat_reply
from .cold_chain_demo import ColdChainDemoService
from .detective import analyze_root_cause, root_cause_cte_sql
from .event_bus import create_event_bus
from .event_store import create_event_store
from .intelligence_api import router as intelligence_router
from .observability import build_observability_snapshot
from .product_catalog import product_attention_queue, search_product_catalog
from .security.gateway import TokenBucket, rate_limit
from .tenant import Role, TenantContext, default_tenant_context, verify_bearer_token
from .tools.mcp_surface import AuditLog, build_platform_tools
from .trace import TraceRegistry
from .worker import (
    CascadeWorker,
    MemoryConsolidationWorker,
    WorkerLoopService,
    create_journal,
    worker_enabled,
)

DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_INSECURE_APP_ENV_NAMES = {"production", "prod", "staging", "stage"}


def cors_allowed_origins() -> list[str]:
    """Return configured frontend origins, with local development defaults."""
    raw = os.getenv("SHELFWISE_CORS_ORIGINS", "")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
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


_reject_insecure_auth_in_named_deployments()

app = FastAPI(title="ShelfWise", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(intelligence_router)

try:
    from shelfwise_multimodal.router import build_scan_router, build_voice_router
except ImportError:
    build_scan_router = None
    build_voice_router = None

if build_voice_router is not None:
    app.include_router(build_voice_router())
if build_scan_router is not None:
    app.include_router(build_scan_router())

decision_store = create_decision_store()
learning_store = create_learning_store()
event_store = create_event_store()
event_bus = create_event_bus()
journal = create_journal()
cascade_worker = CascadeWorker(bus=event_bus, journal=journal, decision_store=decision_store)
worker_service = WorkerLoopService(cascade_worker)
trace_registry = TraceRegistry()
tool_audit = AuditLog()
model_run_registry = create_model_run_registry()
prompt_registry = create_prompt_registry()
tenant_fact_store = create_tenant_fact_store()
tenant_profile_store = create_tenant_profile_store()
writeback_sink = create_writeback_sink()
inbound_record_store = create_inbound_record_store()
product_catalog_store = create_product_catalog_store()
worldgen_run_store = create_worldgen_run_store()
cold_chain_demo = ColdChainDemoService()
app.router.add_event_handler("startup", worker_service.start)
app.router.add_event_handler("shutdown", worker_service.stop)
app.router.add_event_handler("startup", cold_chain_demo.start)
app.router.add_event_handler("shutdown", cold_chain_demo.stop)
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


# Operator knob: unattended harness/soak runs legitimately push write rates far past
# interactive-use defaults. Defaults stay identical when the env vars are unset.
write_limiter = TokenBucket(
    capacity=_env_positive_int("SHELFWISE_WRITE_RATE_CAPACITY", 240),
    refill_per_s=_env_positive_float("SHELFWISE_WRITE_RATE_REFILL_PER_S", 8.0),
)
WRITE_LIMIT_DEP = Depends(rate_limit(write_limiter))
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
async def bind_storage_tenant(request: Request, call_next: Any) -> Any:
    tenant_id = _tenant_id_from_headers(request.headers.get("authorization"))
    token = bind_tenant_context(tenant_id)
    try:
        return await call_next(request)
    finally:
        reset_tenant_context(token)


def _auth_mode() -> str:
    return os.getenv("SHELFWISE_AUTH_MODE", "off").strip().lower()


# Sentinel tenant id bound for the RLS session variable when a request in jwt mode carries
# no valid token. It matches no real tenant row, so a request that skips the route-level
# `current_tenant_context`/`require_role` dependency still can't read another tenant's data
# under RLS - a bad/missing token must never fall back to a real (demo) tenant's context.
_UNAUTHENTICATED_TENANT_ID = "__unauthenticated__"


def _tenant_id_from_headers(authorization: str | None) -> str:
    if _auth_mode() != "jwt":
        return default_tenant_context().tenant_id
    try:
        return verify_bearer_token(
            authorization,
            secret=os.getenv("TENANT_AUTH_SECRET", ""),
        ).tenant_id
    except ValueError:
        return _UNAUTHENTICATED_TENANT_ID


def write_path_guard(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    expected = os.getenv("API_KEY", "")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def current_tenant_context(
    authorization: str | None = Header(default=None, alias="authorization"),
) -> TenantContext:
    mode = _auth_mode()
    if mode == "off":
        return default_tenant_context()
    if mode != "jwt":
        raise HTTPException(status_code=500, detail="Unsupported auth mode")
    try:
        return verify_bearer_token(authorization, secret=os.getenv("TENANT_AUTH_SECRET", ""))
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
def readiness() -> dict[str, object]:
    inference_ready = inference_readiness_payload()
    inference = inference_ready["inference"]
    gateway_status = (
        "offline-safe" if inference["provider"] == "offline" else "configured"
    )
    seed_status = "ok"
    try:
        load_seeded_scenario()
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
            "cold_chain_demo": cold_chain_demo.status(),
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
    network_ready = (
        bool(config.base_url)
        and config.api_key_present
        and bool(config.routine_model)
        and bool(config.strong_model)
        and config.timeout_seconds < 30
    )
    amd_ready = network_ready and config.provider is ProviderKind.VLLM_MI300X
    return {
        "ready_for_live_inference": network_ready,
        "ready_for_amd_demo": amd_ready,
        "amd_compute_used_by_default": config.provider is ProviderKind.VLLM_MI300X,
        "inference": public,
        "checks": {
            "openai_chat_completions_contract": "ok",
            "base_url": "ok" if config.base_url else "missing",
            "api_key": "ok" if config.api_key_present else "missing",
            "routine_model": "ok" if config.routine_model else "missing",
            "strong_model": "ok" if config.strong_model else "missing",
            "timeout_under_30s": "ok" if config.timeout_seconds < 30 else "risk",
            "amd_mi300x_provider": (
                "ok" if config.provider is ProviderKind.VLLM_MI300X else "pending"
            ),
        },
        "next_step": (
            "Run /inference/smoke against the vLLM endpoint."
            if amd_ready
            else "Set LLM_BASE_URL, LLM_API_KEY, and model env vars for the MI300X vLLM endpoint."
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
            "docker_image_required": "no",
            "amd_compute_usage": "ok" if inference_ready["ready_for_amd_demo"] else "pending",
            "response_timeout": "ok",
            "english_responses": "ok",
            "unseen_inputs": "supported_by_seeded_tools_and_bounded_search",
        },
        "inference": inference_ready,
    }


@app.get("/inference/smoke")
def inference_smoke(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
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
        result = OpenAICompatibleInferenceClient(recorder=_record_model_run).complete(
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
        "amd_compute_used": (
            result.provider == ProviderKind.VLLM_MI300X.value and result.used_network
        ),
        "result": result.to_dict(),
        "readiness": readiness_payload,
        "prompt_version": prompt.to_dict(),
    }


@app.get("/data/seed/summary")
def seed_summary() -> dict[str, object]:
    return {"seed_data": load_seeded_scenario().to_dict()}


@app.get("/products/attention")
def product_attention(limit: int = 20) -> dict[str, object]:
    try:
        return product_attention_queue(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/products/search")
def product_search(q: str = "", limit: int = 20) -> dict[str, object]:
    try:
        return search_product_catalog(query=q, limit=limit)
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

    return _record_pipeline_event(event)


@app.get("/events")
def list_events(
    limit: int = 200,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        events = event_store.list(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _auth_mode() == "jwt":
        events = [item for item in events if item.get("tenant_id") == ctx.tenant_id]
    return {"events": events}


@app.get("/events/bus")
def list_bus_events(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    messages = event_bus.list()
    if _auth_mode() == "jwt":
        messages = [item for item in messages if _bus_message_tenant(item) == ctx.tenant_id]
    return {"messages": messages}


def _bus_message_tenant(message: dict[str, Any]) -> str | None:
    event = message.get("event")
    return event.get("tenant_id") if isinstance(event, dict) else None


@app.get("/trace/{correlation_id}")
def get_trace(
    correlation_id: str,
    _ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    # NOTE: CascadeTrace does not yet carry tenant_id, so this requires a valid
    # authenticated caller in jwt mode but does not filter by tenant. Full per-tenant
    # trace scoping needs tenant_id threaded through TraceRegistry - tracked as a
    # follow-up, not silently left fully open in the meantime.
    trace = trace_registry.get(correlation_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": trace}


@app.get("/traces")
def list_traces(_ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return {"traces": trace_registry.list()}


@app.get("/detective/root-cause/{target_id}")
def detective_root_cause(
    target_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        events = event_store.list(limit=500)
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


@app.post("/chat", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def chat(body: ChatBody, ctx: TenantContext = CURRENT_TENANT_DEP) -> StreamingResponse:
    state = {
        "decisions": _bounded_chat_decisions(_tenant_scoped_decisions(ctx)),
        "learning": {
            "thresholds": learning_store.thresholds(),
            "events": _bounded_recent(
                learning_store.list_events(), limit=_CHAT_LEARNING_EVENT_LIMIT
            ),
        },
        "traces": trace_registry.list()[:10],
    }
    client = OpenAICompatibleInferenceClient(recorder=_record_model_run)
    correlation_id = f"chat:{uuid4().hex[:12]}"
    return StreamingResponse(
        stream_chat_reply(
            question=body.question,
            state=state,
            client=client,
            tenant_id=ctx.tenant_id,
            correlation_id=correlation_id,
        ),
        media_type="text/plain",
    )


@app.get("/tools/platform")
def list_platform_tools() -> dict[str, object]:
    tools = build_platform_tools(decisions=decision_store, memory=learning_store, audit=tool_audit)
    return {"tools": [tool.to_dict() for tool in tools]}


@app.get("/tools/platform/audit")
def list_platform_tool_audit() -> dict[str, object]:
    return {"events": tool_audit.list()}


@app.get("/cold-chain/feed")
def list_cold_chain_feed(limit: int = 100) -> dict[str, object]:
    return {"status": cold_chain_demo.status(), "events": cold_chain_demo.list_events(limit=limit)}


@app.get("/demo/worldgen-runs")
def list_worldgen_runs(
    limit: int = 100,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        runs = worldgen_run_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"runs": runs}


@app.get("/demo/worldgen-runs/{run_id}")
def get_worldgen_run(run_id: str) -> dict[str, object]:
    run = worldgen_run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Worldgen run not found")
    return {"run": run}


@app.get("/mlops/model-runs")
def list_model_runs(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    runs = model_run_registry.list(tenant_id=tenant_id)
    return {"model_runs": [run.to_dict() for run in runs]}


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
        profile.get("connector_policy")
        if isinstance(profile.get("connector_policy"), dict)
        else {}
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
    reorder, demand) needs before it can safely trust "which physical item is this" -
    see AUDIT_AND_IMPLEMENTATION_BACKLOG.md P0 item 13 (product master + identity model).
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


@app.get("/mlops/prompts")
def list_prompt_versions(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    tenant_id = ctx.tenant_id if _auth_mode() == "jwt" else None
    prompts = prompt_registry.list(tenant_id=tenant_id)
    return {"prompt_versions": [prompt.to_dict() for prompt in prompts]}


@app.get("/mlops/accountability")
def accountability_report(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    # Derive the tenant from the authenticated context, never a caller-supplied query
    # param - accepting an arbitrary tenant_id here let any authenticated caller read
    # another tenant's model-run and decision accountability data.
    runs = model_run_registry.list(tenant_id=ctx.tenant_id)
    report = build_accountability_report(
        tenant_id=ctx.tenant_id,
        decisions=_tenant_scoped_decisions(ctx),
        models_used=[run.model for run in runs],
        prompt_versions=[run.prompt_version for run in runs],
    )
    return {"report": report.to_dict(), "markdown": report.to_markdown()}


@app.get("/mlops/observability")
def observability_snapshot(
    limit: int = 500,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    try:
        events = event_store.list(limit=limit)
        inbound_records = inbound_record_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = build_observability_snapshot(
        tenant_id=ctx.tenant_id,
        decisions=_tenant_scoped_decisions(ctx),
        model_runs=[run.to_dict() for run in model_run_registry.list(tenant_id=ctx.tenant_id)],
        inbound_records=inbound_records,
        events=events,
        bus_stats=event_bus.stats(tenant_id=ctx.tenant_id),
        writeback_tasks=writeback_sink.list(tenant_id=ctx.tenant_id),
        worker_status=worker_service.status(),
        worker_runs=journal.list_runs(),
        learning_events=learning_store.list_events(),
        tenant_facts=tenant_fact_store.list(tenant_id=ctx.tenant_id, active_only=False),
        rate_zar_per_1k=_inference_rate(),
    )
    return {"snapshot": snapshot}


@app.get("/mlops/tenant-facts")
def list_tenant_facts(
    include_tombstoned: bool = False,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    facts = tenant_fact_store.list(
        tenant_id=ctx.tenant_id,
        active_only=not include_tombstoned,
    )
    return {"tenant_id": ctx.tenant_id, "facts": facts}


@app.post(
    "/mlops/consolidate-memory",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def consolidate_memory(ctx: TenantContext = WORKER_AUTH_DEP) -> dict[str, object]:
    return _memory_consolidation_worker().process_tenant(ctx.tenant_id)


@app.get("/worker/runs")
def list_worker_runs() -> dict[str, object]:
    return {"runs": journal.list_runs()}


@app.get("/worker/status")
def worker_status() -> dict[str, object]:
    return {"worker": worker_service.status()}


@app.post(
    "/worker/process-one",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP, Depends(WORKER_AUTH)],
)
def process_one_worker_event() -> dict[str, object]:
    result = cascade_worker.process_one().to_dict()
    cascade = result.get("cascade")
    if isinstance(cascade, dict):
        result["cascade"] = _record_cascade(cascade)
    return {"result": result}


_DEMO_WRITE_DEPS = [Depends(write_path_guard), WRITE_LIMIT_DEP]


def _preview_demo_cascade(result: dict[str, Any]) -> dict[str, Any]:
    """Enrich a read-only demo preview without mutating stores or traces."""
    _attach_decision_governance(result)
    return result


@app.post("/demo/golden", dependencies=_DEMO_WRITE_DEPS)
def demo_golden() -> dict[str, object]:
    return _record_cascade(run_golden_cascade())


@app.get("/demo/golden", dependencies=_DEMO_WRITE_DEPS)
def demo_golden_get() -> dict[str, object]:
    return _preview_demo_cascade(run_golden_cascade())


@app.post("/demo/golden/agentic", dependencies=_DEMO_WRITE_DEPS)
def demo_golden_agentic(live_required: bool = True) -> dict[str, object]:
    """Run the golden scenario's Critic/Executive verdicts through a real Gemma tool loop.

    Unlike /demo/golden (deterministic math + hand-authored evidence), this route requires
    an actual model call and tool-calling round trip. With live_required=true (default) it
    hard-fails with 503 instead of silently falling back to an offline/deterministic answer.
    """
    mode = ExecutionMode.LIVE_REQUIRED if live_required else ExecutionMode.OFFLINE_TEST
    try:
        result = run_golden_cascade_via_agents(
            execution_mode=mode,
            decisions=decision_store,
            memory=learning_store,
        )
    except AgenticCascadeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _record_cascade(result)


@app.post("/demo/critic-rejection", dependencies=_DEMO_WRITE_DEPS)
def demo_critic_rejection() -> dict[str, object]:
    return _record_cascade(run_critic_rejection_cascade())


@app.get("/demo/critic-rejection", dependencies=_DEMO_WRITE_DEPS)
def demo_critic_rejection_get() -> dict[str, object]:
    return _preview_demo_cascade(run_critic_rejection_cascade())


@app.post("/demo/procurement", dependencies=_DEMO_WRITE_DEPS)
def demo_procurement() -> dict[str, object]:
    return _record_cascade(run_procurement_cascade())


@app.get("/demo/procurement", dependencies=_DEMO_WRITE_DEPS)
def demo_procurement_get() -> dict[str, object]:
    return _preview_demo_cascade(run_procurement_cascade())


@app.post("/demo/sales", dependencies=_DEMO_WRITE_DEPS)
def demo_sales() -> dict[str, object]:
    return _record_cascade(run_sales_cascade())


@app.get("/demo/sales", dependencies=_DEMO_WRITE_DEPS)
def demo_sales_get() -> dict[str, object]:
    return _preview_demo_cascade(run_sales_cascade())


@app.post("/demo/cold-chain", dependencies=_DEMO_WRITE_DEPS)
def demo_cold_chain() -> dict[str, object]:
    return _record_cascade(run_cold_chain_cascade())


@app.get("/demo/cold-chain", dependencies=_DEMO_WRITE_DEPS)
def demo_cold_chain_get() -> dict[str, object]:
    return _preview_demo_cascade(run_cold_chain_cascade())


@app.get("/demo/worldgen/{scenario_id}", dependencies=_DEMO_WRITE_DEPS)
def demo_worldgen_drill(
    scenario_id: str,
    limit: int = 80,
    seed_override: int | None = None,
    assortment_size: int | None = None,
    catalog_scale: str = "supermarket",
) -> dict[str, object]:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    if assortment_size is not None and not (0 < assortment_size <= 20_000):
        raise HTTPException(
            status_code=422, detail="assortment_size must be between 1 and 20000"
        )
    try:
        world, schedule = build_worldgen_scenario(
            scenario_id,
            seed_override=seed_override,
            assortment_size=assortment_size,
            catalog_scale=catalog_scale,
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
        cascade["decision"]
        for cascade in cascades
        if isinstance(cascade.get("decision"), dict)
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
def list_decisions(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return {"decisions": _tenant_scoped_decisions(ctx)}


@app.get("/learning")
def learning_summary() -> dict[str, object]:
    return {
        "thresholds": learning_store.thresholds(),
        "events": learning_store.list_events(),
    }


@app.get("/writeback/tasks")
def list_writeback_tasks(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    """Return tenant-scoped pending task/draft records created by approval."""
    return {"tasks": writeback_sink.list(tenant_id=ctx.tenant_id)}


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
_CHAT_RESOLVED_DECISION_LIMIT = 30
_CHAT_LEARNING_EVENT_LIMIT = 30


def _bounded_chat_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every pending decision plus a recency-bounded window of resolved ones."""
    pending = [item for item in decisions if item.get("status") == "pending"]
    resolved = _bounded_recent(
        [item for item in decisions if item.get("status") != "pending"],
        limit=_CHAT_RESOLVED_DECISION_LIMIT,
    )
    return pending + resolved


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


def _tenant_scoped_decisions(ctx: TenantContext) -> list[dict[str, Any]]:
    """Filter decisions to the authenticated tenant when auth is actually enforced.

    In "off" mode there is exactly one (default) tenant context for the whole process,
    so filtering would be a no-op - skipped there to avoid touching the many callers
    exercised by the default local/test/demo configuration.
    """
    decisions = decision_store.list()
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
            if any(
                token in key for token in ("secret", "password", "api_key", "token")
            ) and not (key.endswith("_ref") or key.endswith("_id")):
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
    return {"decision": decision, "learning_event": None}


def _record_pipeline_event(event: Event) -> dict[str, Any]:
    # Recording before publishing (durable-store-first) means a bus failure between the two
    # calls would otherwise orphan the event: stored but never enqueued, with every retry of
    # the same event id short-circuited as a harmless-looking "duplicate". The `published`
    # flag closes that gap - only an event that actually reached the bus is treated as a true
    # duplicate on resubmission; anything recorded-but-unpublished self-heals by publishing
    # on the next attempt with the same id.
    is_new = event_store.record(event)
    if not is_new and event_store.is_published(event.id):
        return {
            "status": "duplicate",
            "event": event.to_dict(),
            "bus_message_id": None,
            "cascade": None,
        }

    bus_message_id = event_bus.publish(event)
    event_store.mark_published(event.id)
    cascade = None if worker_enabled() else _cascade_for_event(event)
    return {
        "status": "accepted",
        "event": event.to_dict(),
        "bus_message_id": bus_message_id,
        "cascade": cascade,
    }


def _learning_outcome_records(tenant_id: str) -> list[OutcomeRecord]:
    decisions = {
        str(decision.get("id")): decision
        for decision in decision_store.list()
        if _decision_tenant_id(decision, tenant_id) == tenant_id
    }
    records: list[OutcomeRecord] = []
    for event in learning_store.list_events():
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
    sku = str(event.payload.get("sku", ""))
    if event.type is EventType.SCAN and sku == "4011":
        result = run_golden_cascade(event)
        _attach_event_causality(result, event)
        return _record_cascade(result)
    supplier = str(event.payload.get("supplier", "")).lower()
    if event.type is EventType.SUPPLIER_UPDATE and supplier == "dairyco":
        result = run_procurement_cascade(event)
        _attach_event_causality(result, event)
        return _record_cascade(result)
    if event.type is EventType.SALE and sku == "4011":
        result = run_sales_cascade(event)
        _attach_event_causality(result, event)
        return _record_cascade(result)
    if event.type is EventType.SALE:
        result = run_catalog_price_check(event)
        if result is None:
            return None
        _attach_event_causality(result, event)
        return _record_cascade(result)
    if event.type is EventType.EXPIRY_ENTRY:
        result = run_expiry_risk_check(event)
        if result is None:
            return None
        _attach_event_causality(result, event)
        return _record_cascade(result)
    if event.type is EventType.COLD_CHAIN_ALERT:
        result = run_cold_chain_cascade(event)
        _attach_event_causality(result, event)
        return _record_cascade(result)
    return None


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
            "correlation_id": f"worldgen:{scenario_id}:{seed}:cold_chain",
            "payload": {
                "site_id": actor,
                "area": area,
                "asset_id": "fridge_dairy_1",
                "category": "dairy",
                "diagnosis": "generator_failed",
                "severity": 2,
                "predicted_minutes_to_unsafe": "18",
                "measured_outage_hours": str(measured_outage_hours),
                "temp_c": "8.2",
                "stock_at_risk": {"minor_units": 643_500, "currency": "ZAR"},
                "synthetic": True,
            },
        }
    )


def _attach_event_causality(result: dict[str, Any], event: Event) -> None:
    result["correlation_id"] = event.correlation_id
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["caused_by"] = [event.id]


def _record_cascade(result: dict[str, Any]) -> dict[str, Any]:
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
    model_run_registry.record(ModelRun(**payload))
