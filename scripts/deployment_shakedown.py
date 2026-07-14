"""Run a bounded public-origin HTTP shakedown for a deployed ShelfWise stack.

This harness deliberately uses only the public HTTP surface.  It is separate from the
in-process full-system harness, which remains the exhaustive domain regression suite.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

SCHEMA_VERSION = "deployment-shakedown/v1"
DEFAULT_QUESTIONS = (
    "What is the current stock position for an unseen product variant?",
    "What should the manager check first for an unseen cold-chain alert?",
)
SCENARIO_PATH = "/demo/golden"
SESSION_COOKIE_NAME = "shelfwise_session"


@dataclass(frozen=True)
class DeploymentShakedownConfig:
    """Validated inputs for one public-origin shakedown."""

    base_url: str
    cycles: int = 3
    request_timeout: float = 10.0
    startup_deadline: float = 60.0
    duration_seconds: float = 0.0
    live_required: bool = False
    api_key_env: str = "SHELFWISE_API_KEY"
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        """Reject unsafe URLs and unbounded or invalid execution budgets."""
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http or https URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        if not 1 <= self.cycles <= 1_000:
            raise ValueError("cycles must be between 1 and 1000")
        if not 0 < self.request_timeout < 30:
            raise ValueError("request_timeout must be below 30 seconds")
        if not 0 < self.startup_deadline <= 60:
            raise ValueError("startup_deadline must be between 0 and 60 seconds")
        if not 0 <= self.duration_seconds <= 900:
            raise ValueError("duration_seconds must be between 0 and 900 seconds")
        if not 0 < self.poll_interval <= 5:
            raise ValueError("poll_interval must be between 0 and 5 seconds")


@dataclass(frozen=True)
class ProbeReceipt:
    """Secret-free timing and status metadata for one HTTP request."""

    method: str
    path: str
    status_code: int | None
    elapsed_ms: float
    ok: bool
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe probe metadata without headers or bodies."""
        return asdict(self)


@dataclass(frozen=True)
class RouteReceipt:
    """Outcome for a public route, including the expected status contract."""

    method: str
    path: str
    status_code: int | None
    expected_status: tuple[int, ...]
    ok: bool
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize a route result with a stable list for expected statuses."""
        result = asdict(self)
        result["expected_status"] = list(self.expected_status)
        return result


@dataclass(frozen=True)
class AuthReceipt:
    """Describe session establishment without retaining a cookie or API-key value."""

    session_route_ok: bool
    mode: str
    cookie_name: str
    cookie_received: bool
    api_key_configured: bool
    tenant_id: str | None


@dataclass(frozen=True)
class ReadinessReceipt:
    """Record deployment readiness fields that are safe to publish."""

    route_ok: bool
    ready: bool
    auth_mode: str | None
    inference_provider: str | None
    amd_demo_ready: bool
    storage_backends: tuple[str, ...]
    event_bus: str | None


@dataclass(frozen=True)
class HitlReceipt:
    """Summarize decision identity and approval/rejection transition checks."""

    observed_decision_ids: tuple[str, ...]
    unique_decision_ids: bool
    tenant_mismatches: int
    approvals: int
    rejections: int
    transition_mismatches: tuple[str, ...]


@dataclass(frozen=True)
class LearningReceipt:
    """Summarize learning events and positive threshold movement."""

    route_ok: bool
    event_count: int
    movements: int
    movements_expected: int


@dataclass(frozen=True)
class WritebackReceipt:
    """Summarize recommend-only write-back tasks created by approval."""

    route_ok: bool
    task_count: int
    pending_external_writes: int
    approved_decisions: int


@dataclass(frozen=True)
class ChatReceipt:
    """Record safe chat header evidence, including replay and fallback counts."""

    calls: int
    model_answers: int
    fallback_answers: int
    replay_checks: int
    replay_matches: int
    headers: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class DeploymentReceipt:
    """Typed, secret-free receipt for a deployed-topology shakedown."""

    generated_at: str
    schema_version: str
    verdict: str
    mode: str
    base_url_host: str
    cycles_requested: int
    cycles_completed: int
    duration_seconds: float
    startup: dict[str, object]
    frontend: dict[str, object]
    auth: AuthReceipt
    readiness: ReadinessReceipt
    routes: tuple[RouteReceipt, ...]
    probes: tuple[ProbeReceipt, ...]
    hitl: HitlReceipt
    learning: LearningReceipt
    writeback: WritebackReceipt
    chat: ChatReceipt
    failures: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """Serialize the receipt while preserving its secret-free field boundary."""
        result = asdict(self)
        result["routes"] = [item.to_dict() for item in self.routes]
        result["probes"] = [item.to_dict() for item in self.probes]
        result["auth"] = asdict(self.auth)
        result["readiness"] = asdict(self.readiness)
        result["hitl"] = asdict(self.hitl)
        result["learning"] = asdict(self.learning)
        result["writeback"] = asdict(self.writeback)
        result["chat"] = asdict(self.chat)
        result["failures"] = list(self.failures)
        return result


@dataclass(frozen=True)
class _Response:
    """Internal response envelope that is never written to a receipt."""

    probe: ProbeReceipt
    payload: object
    text: str
    headers: dict[str, str]


def _safe_json(response: httpx.Response) -> object:
    """Decode JSON when present, returning an empty object for non-JSON responses."""
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return {}


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    timeout: float,
) -> _Response:
    """Make one bounded request and convert transport failures into typed probe metadata."""
    started = time.perf_counter()
    try:
        response = client.request(method, path, json=payload, timeout=timeout)
    except httpx.TimeoutException:
        probe = ProbeReceipt(method, path, None, _elapsed_ms(started), False, "request_timeout")
        return _Response(probe, {}, "", {})
    except httpx.HTTPError:
        probe = ProbeReceipt(method, path, None, _elapsed_ms(started), False, "request_error")
        return _Response(probe, {}, "", {})
    text = response.text
    probe = ProbeReceipt(
        method,
        path,
        response.status_code,
        _elapsed_ms(started),
        200 <= response.status_code < 300,
    )
    return _Response(probe, _safe_json(response), text, _safe_headers(response.headers))


def _elapsed_ms(started: float) -> float:
    """Return rounded monotonic elapsed time for a probe."""
    return round((time.perf_counter() - started) * 1000, 2)


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    """Keep only non-secret chat evidence headers."""
    allowed = {
        "x-shelfwise-answer-source",
        "x-shelfwise-correlation-id",
        "x-shelfwise-model",
        "x-shelfwise-provider",
        "x-shelfwise-replayed",
        "content-type",
    }
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


def _add_route(
    routes: list[RouteReceipt],
    response: _Response,
    *,
    expected: tuple[int, ...] = (200,),
) -> None:
    """Convert a probe into a route receipt and retain a stable route-failure code."""
    ok = response.probe.status_code in expected
    failure_code = response.probe.failure_code
    if not ok and failure_code is None:
        failure_code = (
            f"route_failure:{response.probe.method} {response.probe.path}"
            f":status={response.probe.status_code}"
        )
    routes.append(
        RouteReceipt(
            method=response.probe.method,
            path=response.probe.path,
            status_code=response.probe.status_code,
            expected_status=expected,
            ok=ok,
            failure_code=failure_code,
        )
    )


def _failure_from_response(response: _Response) -> str | None:
    """Return a request failure code, if the transport failed."""
    return response.probe.failure_code


def _cookie_received(client: httpx.Client) -> bool:
    """Check only cookie presence; never return the opaque cookie value."""
    return bool(client.cookies.get(SESSION_COOKIE_NAME))


def _session_context(payload: object) -> tuple[str, str]:
    """Extract tenant and mode from the safe public session payload."""
    if not isinstance(payload, dict):
        return "", "unknown"
    session = payload.get("session")
    session = session if isinstance(session, dict) else {}
    return str(session.get("tenant_id") or ""), str(payload.get("mode") or "unknown")


def _decision_from(payload: object) -> dict[str, object]:
    """Extract a decision object from a public scenario response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("decision"), dict):
        return {}
    return payload["decision"]


def _positive_learning_movement(event: object) -> bool:
    """Return true when a learning event records a positive threshold movement."""
    if not isinstance(event, dict):
        return False
    try:
        if int(event.get("delta_units") or 0) > 0:
            return True
        return int(event.get("updated_threshold") or 0) > int(
            event.get("previous_threshold") or 0
        )
    except (TypeError, ValueError):
        return False


def _chat_metadata(headers: dict[str, str]) -> dict[str, str]:
    """Project chat headers into receipt-safe metadata and omit empty values."""
    return {key: value for key, value in headers.items() if value}


def _new_chat_payload(cycle: int, question: str) -> dict[str, object]:
    """Create an unseen chat identity for one cycle without embedding credentials."""
    return {
        "question": question,
        "conversation_id": f"deployment_{cycle}_{uuid4().hex}",
        "message_id": f"message_{cycle}_{uuid4().hex}",
    }


def _record_chat(
    client: httpx.Client,
    routes: list[RouteReceipt],
    probes: list[ProbeReceipt],
    failures: list[str],
    *,
    payload: dict[str, object],
    config: DeploymentShakedownConfig,
    metadata: list[dict[str, str]],
    counters: dict[str, int],
) -> None:
    """Probe a fresh chat, a replay, and the live/fallback header contract."""
    first = _request(client, "POST", "/chat", payload=payload, timeout=config.request_timeout)
    probes.append(first.probe)
    _add_route(routes, first)
    if not first.probe.ok:
        failures.append(first.probe.failure_code or "route_failure:POST /chat")
        return
    headers = first.headers
    answer_source = headers.get("x-shelfwise-answer-source", "")
    provider = headers.get("x-shelfwise-provider", "")
    correlation_id = headers.get("x-shelfwise-correlation-id", "")
    if not first.text.strip():
        failures.append("chat_empty_answer")
    if not correlation_id:
        failures.append("chat_missing_correlation_id")
    if headers.get("x-shelfwise-replayed") != "false":
        failures.append("chat_fresh_request_replayed")
    if config.live_required:
        if provider != "vllm_mi300x":
            failures.append(f"chat_provider_not_amd:{provider or 'missing'}")
        if answer_source != "model":
            failures.append(f"chat_not_model_backed:{answer_source or 'missing'}")
        if not headers.get("x-shelfwise-model", "").startswith("google/gemma-4"):
            failures.append("chat_model_header_missing_or_unexpected")
    counters["calls"] += 1
    if answer_source == "model":
        counters["model_answers"] += 1
    else:
        counters["fallback_answers"] += 1
    metadata.append(_chat_metadata(headers))

    replay = _request(client, "POST", "/chat", payload=payload, timeout=config.request_timeout)
    probes.append(replay.probe)
    _add_route(routes, replay)
    counters["replay_checks"] += 1
    if replay.probe.ok and replay.headers.get("x-shelfwise-replayed") == "true":
        counters["replay_matches"] += 1
    else:
        failures.append("chat_replay_mismatch")
    if replay.probe.failure_code:
        failures.append(replay.probe.failure_code)


def _poll_health(
    client: httpx.Client,
    routes: list[RouteReceipt],
    probes: list[ProbeReceipt],
    failures: list[str],
    config: DeploymentShakedownConfig,
) -> tuple[dict[str, object], float]:
    """Poll health over HTTP until ready or the startup budget is exhausted."""
    started = time.perf_counter()
    health: dict[str, object] = {}
    while time.perf_counter() - started < config.startup_deadline:
        response = _request(client, "GET", "/health", timeout=config.request_timeout)
        probes.append(response.probe)
        _add_route(routes, response)
        if response.probe.ok and isinstance(response.payload, dict):
            health = response.payload
            if health.get("ok") is True:
                return health, round(time.perf_counter() - started, 3)
        if response.probe.failure_code == "request_timeout":
            failures.append("startup_timeout")
        if response.probe.failure_code:
            failures.append(response.probe.failure_code)
        time.sleep(min(config.poll_interval, max(0.0, config.startup_deadline)))
    failures.append("startup_deadline_exceeded")
    return health, round(time.perf_counter() - started, 3)


def run_deployment_shakedown(config: DeploymentShakedownConfig) -> DeploymentReceipt:
    """Execute the public-origin checks and return PASS/FAIL instead of raising raw errors."""
    started = time.perf_counter()
    routes: list[RouteReceipt] = []
    probes: list[ProbeReceipt] = []
    failures: list[str] = []
    decisions: list[str] = []
    transition_mismatches: list[str] = []
    tenant_mismatches = 0
    approvals = 0
    rejections = 0
    expected_learning = 0
    observed_learning = 0
    api_key_configured = bool(os.getenv(config.api_key_env, "").strip())
    mode = "live-required" if config.live_required else "short"
    timeout = httpx.Timeout(config.request_timeout)
    headers = {"x-api-key": os.getenv(config.api_key_env, "")} if api_key_configured else {}
    client = httpx.Client(
        base_url=config.base_url.rstrip("/"),
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    )

    health: dict[str, object] = {}
    frontend: dict[str, object] = {"status_code": None, "ok": False, "content_type": ""}
    session_ok = False
    cookie_received = False
    session_mode = "unknown"
    tenant_id = ""
    readiness_ok = False
    readiness: dict[str, object] = {}
    chat_metadata: list[dict[str, str]] = []
    chat_counters = {
        "calls": 0,
        "model_answers": 0,
        "fallback_answers": 0,
        "replay_checks": 0,
        "replay_matches": 0,
    }
    scenario_counters = {
        "approvals": 0,
        "rejections": 0,
        "expected_learning": 0,
        "observed_learning": 0,
    }
    state_counters = {
        "learning_events": 0,
        "writeback_tasks": 0,
        "pending_external_writes": 0,
        "learning_route_ok": 0,
        "writeback_route_ok": 0,
    }
    startup_seconds = 0.0

    try:
        health, startup_seconds = _poll_health(client, routes, probes, failures, config)

        frontend_response = _request(client, "GET", "/", timeout=config.request_timeout)
        probes.append(frontend_response.probe)
        _add_route(routes, frontend_response)
        frontend = {
            "status_code": frontend_response.probe.status_code,
            "ok": frontend_response.probe.ok,
            "content_type": frontend_response.headers.get("content-type", ""),
        }
        if frontend_response.probe.failure_code:
            failures.append(frontend_response.probe.failure_code)

        session_response = _request(
            client, "POST", "/auth/session", timeout=config.request_timeout
        )
        probes.append(session_response.probe)
        _add_route(routes, session_response)
        session_ok = session_response.probe.ok
        tenant_id, session_mode = _session_context(session_response.payload)
        cookie_received = _cookie_received(client)
        if not session_ok:
            failures.append(
                session_response.probe.failure_code or "route_failure:POST /auth/session"
            )
        if not cookie_received:
            failures.append("auth_session_cookie_missing")

        readiness_response = _request(client, "GET", "/readiness", timeout=config.request_timeout)
        probes.append(readiness_response.probe)
        _add_route(routes, readiness_response)
        readiness_ok = readiness_response.probe.ok
        readiness = (
            readiness_response.payload
            if isinstance(readiness_response.payload, dict)
            else {}
        )
        if not readiness_ok:
            failures.append(
                readiness_response.probe.failure_code or "route_failure:GET /readiness"
            )
        _check_readiness(readiness, config, failures)

        if session_ok and readiness_ok:
            _run_scenarios(
                client,
                routes,
                probes,
                failures,
                config,
                tenant_id,
                decisions,
                transition_mismatches,
                chat_metadata,
                chat_counters,
                counters=scenario_counters,
            )
            approvals = scenario_counters["approvals"]
            rejections = scenario_counters["rejections"]
            expected_learning = scenario_counters["expected_learning"]
            observed_learning = scenario_counters["observed_learning"]

            _probe_state_routes(
                client,
                routes,
                probes,
                failures,
                config,
                decisions,
                approvals,
                expected_learning,
                observed_learning,
                tenant_id,
                transition_mismatches,
                chat_metadata,
                chat_counters,
                state_counters,
            )
    except (httpx.HTTPError, ValueError) as exc:
        failures.append(f"harness_error:{type(exc).__name__}")
    finally:
        client.close()

    unique_decisions = len(decisions) == len(set(decisions))
    if not unique_decisions:
        failures.append("decision_id_reuse")
    tenant_mismatch_ids = {
        item.split(":", 1)[1]
        for item in failures
        if item.startswith("tenant_mismatch:")
    }
    tenant_mismatches = len(tenant_mismatch_ids)
    if chat_counters["replay_checks"] != chat_counters["replay_matches"]:
        failures.append("chat_replay_integrity_failure")
    if config.live_required and chat_counters["fallback_answers"]:
        failures.append(f"live_chat_fallbacks:{chat_counters['fallback_answers']}")
    failures = sorted(set(failures))
    receipt = DeploymentReceipt(
        generated_at=datetime.now(UTC).isoformat(),
        schema_version=SCHEMA_VERSION,
        verdict="PASS" if not failures else "FAIL",
        mode=mode,
        base_url_host=urlparse(config.base_url).netloc,
        cycles_requested=config.cycles,
        cycles_completed=_completed_cycles(probes),
        duration_seconds=round(time.perf_counter() - started, 3),
        startup={"health": health.get("ok") is True, "elapsed_seconds": startup_seconds},
        frontend=frontend,
        auth=AuthReceipt(
            session_route_ok=session_ok,
            mode=session_mode,
            cookie_name=SESSION_COOKIE_NAME,
            cookie_received=cookie_received,
            api_key_configured=api_key_configured,
            tenant_id=tenant_id or None,
        ),
        readiness=_readiness_receipt(readiness, readiness_ok),
        routes=tuple(routes),
        probes=tuple(probes),
        hitl=HitlReceipt(
            observed_decision_ids=tuple(decisions),
            unique_decision_ids=unique_decisions,
            tenant_mismatches=tenant_mismatches,
            approvals=approvals,
            rejections=rejections,
            transition_mismatches=tuple(transition_mismatches),
        ),
        learning=LearningReceipt(
            route_ok=bool(state_counters["learning_route_ok"]),
            event_count=state_counters["learning_events"],
            movements=observed_learning,
            movements_expected=expected_learning,
        ),
        writeback=WritebackReceipt(
            route_ok=bool(state_counters["writeback_route_ok"]),
            task_count=state_counters["writeback_tasks"],
            pending_external_writes=state_counters["pending_external_writes"],
            approved_decisions=approvals,
        ),
        chat=ChatReceipt(
            calls=chat_counters["calls"],
            model_answers=chat_counters["model_answers"],
            fallback_answers=chat_counters["fallback_answers"],
            replay_checks=chat_counters["replay_checks"],
            replay_matches=chat_counters["replay_matches"],
            headers=tuple(chat_metadata),
        ),
        failures=tuple(failures),
    )
    return receipt


def _check_readiness(
    payload: dict[str, object], config: DeploymentShakedownConfig, failures: list[str]
) -> None:
    """Validate readiness without requiring live inference in short mode."""
    if payload.get("ready") is not True:
        failures.append("readiness_not_ready")
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    if config.live_required:
        if checks.get("auth_mode") != "jwt":
            failures.append("readiness_auth_mode_not_jwt")
        if checks.get("tenant_auth_secret_configured") is not True:
            failures.append("readiness_tenant_secret_unconfigured")
        if checks.get("amd_demo") != "ok":
            failures.append("readiness_amd_demo_not_ready")


def _readiness_receipt(payload: dict[str, object], route_ok: bool) -> ReadinessReceipt:
    """Project safe readiness and backend names into the typed receipt."""
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    storage = tuple(
        str(checks[key])
        for key in ("decision_store", "learning_store", "event_store", "journal", "writeback_sink")
        if checks.get(key)
    )
    inference = payload.get("inference") if isinstance(payload.get("inference"), dict) else {}
    return ReadinessReceipt(
        route_ok=route_ok,
        ready=payload.get("ready") is True,
        auth_mode=str(checks.get("auth_mode")) if checks.get("auth_mode") is not None else None,
        inference_provider=(
            str(inference.get("provider")) if inference.get("provider") is not None else None
        ),
        amd_demo_ready=checks.get("amd_demo") == "ok",
        storage_backends=storage,
        event_bus=str(checks.get("event_bus")) if checks.get("event_bus") else None,
    )


def _run_scenarios(
    client: httpx.Client,
    routes: list[RouteReceipt],
    probes: list[ProbeReceipt],
    failures: list[str],
    config: DeploymentShakedownConfig,
    tenant_id: str,
    decision_ids: list[str],
    transition_mismatches: list[str],
    chat_metadata: list[dict[str, str]],
    chat_counters: dict[str, int],
    *,
    counters: dict[str, int],
) -> None:
    """Run bounded generated scenarios, resolve each decision, and ask unseen chat questions."""
    deadline = time.perf_counter() + config.duration_seconds if config.duration_seconds else None
    cycle = 0
    while cycle < config.cycles and (deadline is None or time.perf_counter() < deadline):
        cycle += 1
        scenario = _request(client, "POST", SCENARIO_PATH, timeout=config.request_timeout)
        probes.append(scenario.probe)
        _add_route(routes, scenario)
        if not scenario.probe.ok:
            failures.append(scenario.probe.failure_code or "route_failure:POST /demo/golden")
            continue
        decision = _decision_from(scenario.payload)
        decision_id = str(decision.get("id") or "")
        if not decision_id:
            failures.append("decision_id_missing")
            continue
        decision_ids.append(decision_id)
        returned_tenant = str(decision.get("tenant_id") or tenant_id)
        if tenant_id and returned_tenant != tenant_id:
            failures.append(f"tenant_mismatch:{decision_id}")
        action = "approve" if cycle % 2 else "reject"
        transition = _request(
            client,
            "POST",
            f"/decisions/{decision_id}/{action}",
            payload={},
            timeout=config.request_timeout,
        )
        probes.append(transition.probe)
        _add_route(routes, transition)
        body = transition.payload if isinstance(transition.payload, dict) else {}
        resolved = body.get("decision") if isinstance(body.get("decision"), dict) else {}
        expected_status = "approved" if action == "approve" else "rejected"
        if (
            not transition.probe.ok
            or str(resolved.get("id") or "") != decision_id
            or str(resolved.get("status") or "") != expected_status
        ):
            mismatch = f"{decision_id}:{action}:status={resolved.get('status')}"
            transition_mismatches.append(mismatch)
            failures.append(f"hitl_transition_mismatch:{mismatch}")
        elif action == "approve":
            counters["approvals"] += 1
            learning_event = body.get("learning_event")
            if _positive_learning_movement(learning_event):
                counters["expected_learning"] += 1
                counters["observed_learning"] += 1
            else:
                failures.append(f"learning_noop:{decision_id}")
        else:
            counters["rejections"] += 1
        question = DEFAULT_QUESTIONS[(cycle - 1) % len(DEFAULT_QUESTIONS)]
        _record_chat(
            client,
            routes,
            probes,
            failures,
            payload=_new_chat_payload(cycle, question),
            config=config,
            metadata=chat_metadata,
            counters=chat_counters,
        )
        if config.duration_seconds:
            time.sleep(min(1.0, config.poll_interval))


def _probe_state_routes(
    client: httpx.Client,
    routes: list[RouteReceipt],
    probes: list[ProbeReceipt],
    failures: list[str],
    config: DeploymentShakedownConfig,
    decision_ids: list[str],
    approvals: int,
    expected_learning: int,
    observed_learning: int,
    tenant_id: str,
    transition_mismatches: list[str],
    chat_metadata: list[dict[str, str]],
    chat_counters: dict[str, int],
    state_counters: dict[str, int],
) -> None:
    """Check decision listing, learning, write-back, observability, and a second unseen chat."""
    del approvals, expected_learning, observed_learning
    del transition_mismatches, chat_metadata, chat_counters
    for path in ("/decisions", "/learning", "/writeback/tasks", "/mlops/observability"):
        response = _request(client, "GET", path, timeout=config.request_timeout)
        probes.append(response.probe)
        _add_route(routes, response)
        if not response.probe.ok:
            failures.append(f"{path.lstrip('/').replace('/', '_')}_route_failure")
        if path == "/decisions" and isinstance(response.payload, dict):
            listed = response.payload.get("decisions")
            if isinstance(listed, list):
                for decision in listed:
                    if not isinstance(decision, dict):
                        continue
                    listed_tenant = str(decision.get("tenant_id") or tenant_id)
                    if tenant_id and listed_tenant != tenant_id:
                        failures.append(f"tenant_mismatch:{decision.get('id') or 'unknown'}")
        if path == "/learning":
            events = response.payload.get("events") if isinstance(response.payload, dict) else []
            state_counters["learning_route_ok"] = int(response.probe.ok)
            state_counters["learning_events"] = len(events) if isinstance(events, list) else 0
            if not isinstance(events, list):
                failures.append("learning_payload_invalid")
            elif not events:
                failures.append("learning_events_missing")
        if path == "/writeback/tasks":
            tasks = response.payload.get("tasks") if isinstance(response.payload, dict) else []
            state_counters["writeback_route_ok"] = int(response.probe.ok)
            state_counters["writeback_tasks"] = len(tasks) if isinstance(tasks, list) else 0
            state_counters["pending_external_writes"] = sum(
                1
                for item in tasks
                if isinstance(item, dict) and item.get("status") == "pending_external_write"
            ) if isinstance(tasks, list) else 0
            if not isinstance(tasks, list):
                failures.append("writeback_payload_invalid")
            elif any(
                item.get("status") != "pending_external_write"
                for item in tasks
                if isinstance(item, dict)
            ):
                failures.append("writeback_status_mismatch")


def _completed_cycles(probes: list[ProbeReceipt]) -> int:
    """Estimate completed cycles from successful public scenario posts."""
    return sum(1 for probe in probes if probe.path == SCENARIO_PATH and probe.ok)


def write_receipt(receipt: DeploymentReceipt, output: Path | None) -> str:
    """Serialize and optionally write a receipt using stable JSON formatting."""
    serialized = json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    return serialized


def main() -> int:
    """Parse CLI arguments, run the shakedown, and return its pass/fail exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public frontend/backend origin")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--request-deadline", type=float, default=10.0)
    parser.add_argument("--startup-deadline", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--api-key-env", default="SHELFWISE_API_KEY")
    parser.add_argument("--live-required", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        config = DeploymentShakedownConfig(
            base_url=args.base_url,
            cycles=args.cycles,
            request_timeout=args.request_deadline,
            startup_deadline=args.startup_deadline,
            duration_seconds=args.duration_seconds,
            live_required=args.live_required,
            api_key_env=args.api_key_env,
            poll_interval=args.poll_interval,
        )
    except ValueError as exc:
        parser.error(str(exc))
    receipt = run_deployment_shakedown(config)
    print(write_receipt(receipt, args.output), end="")
    return 0 if receipt.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
