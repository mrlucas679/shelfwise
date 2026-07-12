from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from shelfwise_backend.app import (
    app,
    cold_chain_demo,
    decision_store,
    event_bus,
    event_store,
    journal,
    learning_store,
    model_run_registry,
    prompt_registry,
    tenant_fact_store,
    tool_audit,
    trace_registry,
    world_snapshot_store,
    worldgen_run_store,
    write_limiter,
    writeback_sink,
)

_EXPECTED_GOLDEN_AGENTS = [
    "inventory",
    "demand",
    "expiry",
    "opportunity",
    "simulation",
    "critic",
    "executive",
]
_REQUIRED_TRACE_SPANS = {
    "decision_science.forecast_demand",
    "decision_science.score_expiry_risk",
    "decision_science.simulate_markdown",
}


@dataclass(frozen=True, slots=True)
class EvalCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class EvalReport:
    checks: list[EvalCheck]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for check in self.checks if check.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_backend_eval(*, token_ceiling: int = 24_000) -> EvalReport:
    """Drive the real FastAPI app through the demo-critical eval checks."""
    _reset_in_memory_demo_state()
    client = TestClient(app)
    checks: list[EvalCheck] = []

    readiness = client.get("/readiness")
    _record(
        checks,
        "readiness",
        readiness.status_code == 200 and readiness.json().get("ready") is True,
        _status_detail(readiness),
    )

    golden = client.post("/demo/golden", headers=_write_headers())
    _record(checks, "golden_http", golden.status_code == 200, _status_detail(golden))
    golden_body = golden.json() if golden.status_code == 200 else {}
    _check_golden_cascade(checks, client, golden_body, token_ceiling=token_ceiling)

    critic = client.post("/demo/critic-rejection", headers=_write_headers())
    _record(checks, "critic_rejection_http", critic.status_code == 200, _status_detail(critic))
    if critic.status_code == 200:
        _check_critic_rejection(checks, critic.json())

    tools = client.get("/tools/platform")
    _record(checks, "tool_catalog_http", tools.status_code == 200, _status_detail(tools))
    if tools.status_code == 200:
        body = tools.json()
        tool_rows = body.get("tools") if isinstance(body.get("tools"), list) else []
        _record(
            checks,
            "tool_catalog_read_only",
            bool(tool_rows) and all(tool.get("read_only") is True for tool in tool_rows),
            f"{len(tool_rows)} tools exposed",
        )

    _check_product_catalog(checks, client)
    _check_store_intelligence_tools(checks, client)
    _check_inference_and_submission(checks, client)

    return EvalReport(checks=checks)


def run_eval(*, token_ceiling: int = 24_000) -> dict[str, Any]:
    """Return the legacy dictionary scorecard for notebook compatibility."""
    report = run_backend_eval(token_ceiling=token_ceiling)
    return {
        "passed": report.passed_count,
        "failed": report.total_count - report.passed_count,
        "checks": [check.to_dict() for check in report.checks],
    }


def format_report(result: dict[str, Any]) -> str:
    """Format a legacy eval dictionary as a compact human-readable scorecard."""
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    status = "PASS" if int(result.get("failed") or 0) == 0 else "FAIL"
    lines = [f"SHELFWISE EVAL {status}: {result.get('passed', 0)}/{len(checks)} checks passed"]
    for item in checks:
        if not isinstance(item, dict):
            continue
        marker = "PASS" if item.get("passed") else "FAIL"
        lines.append(f"{marker} {item.get('name')}: {item.get('detail')}")
    return "\n".join(lines)


def _check_golden_cascade(
    checks: list[EvalCheck],
    client: TestClient,
    body: dict[str, Any],
    *,
    token_ceiling: int,
) -> None:
    decision = body.get("decision") if isinstance(body.get("decision"), dict) else {}
    evidence = body.get("evidence") if isinstance(body.get("evidence"), list) else []
    agents = [str(item.get("agent")) for item in evidence if isinstance(item, dict)]
    action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
    expected = (
        decision.get("expected_outcome")
        if isinstance(decision.get("expected_outcome"), dict)
        else {}
    )
    economics = decision.get("economics") if isinstance(decision.get("economics"), dict) else {}

    _record(
        checks,
        "golden_agents",
        agents == _EXPECTED_GOLDEN_AGENTS,
        f"agents={agents}",
    )
    _record(
        checks,
        "golden_pending_hitl",
        decision.get("status") == "pending"
        and action.get("type") == "apply_markdown"
        and action.get("risk_tier") == "high",
        f"status={decision.get('status')} action={action.get('type')}",
    )
    _record(
        checks,
        "golden_money",
        int(expected.get("incremental_profit_minor_units") or 0) > 0,
        f"incremental_profit_minor_units={expected.get('incremental_profit_minor_units')}",
    )
    _record(
        checks,
        "grounded_evidence",
        bool(evidence) and all(item.get("sources") for item in evidence if isinstance(item, dict)),
        f"evidence_count={len(evidence)}",
    )

    total_tokens = int(economics.get("total_tokens") or 0)
    _record(
        checks,
        "token_ceiling",
        0 < total_tokens <= token_ceiling,
        f"total_tokens={total_tokens} ceiling={token_ceiling}",
    )

    correlation_id = str(body.get("correlation_id") or "")
    trace = client.get(f"/trace/{correlation_id}") if correlation_id else None
    trace_ok = False
    trace_detail = "missing correlation_id"
    if trace is not None:
        trace_detail = _status_detail(trace)
        if trace.status_code == 200:
            trace_body = trace.json().get("trace", {})
            span_names = {
                str(span.get("name"))
                for span in trace_body.get("spans", [])
                if isinstance(span, dict)
            }
            trace_ok = (
                trace_body.get("decision_id") == decision.get("id")
                and _REQUIRED_TRACE_SPANS.issubset(span_names)
            )
            trace_detail = f"spans={sorted(span_names)}"
    _record(checks, "trace_chain", trace_ok, trace_detail)

    approve = client.post(
        f"/decisions/{decision.get('id')}/approve",
        headers=_write_headers(),
    )
    _record(checks, "hitl_approve_http", approve.status_code == 200, _status_detail(approve))
    if approve.status_code == 200:
        approved_body = approve.json()
        approved = (
            approved_body.get("decision")
            if isinstance(approved_body.get("decision"), dict)
            else {}
        )
        learning = (
            approved_body.get("learning_event")
            if isinstance(approved_body.get("learning_event"), dict)
            else {}
        )
        write_back = (
            approved.get("write_back") if isinstance(approved.get("write_back"), dict) else {}
        )
        _record(
            checks,
            "pending_to_approved",
            approved.get("status") == "approved"
            and write_back.get("status") == "pending_external_write",
            f"status={approved.get('status')} write_back={write_back.get('status')}",
        )
        _record(
            checks,
            "learning_moment",
            int(learning.get("updated_threshold") or 0)
            >= int(learning.get("previous_threshold") or 0),
            f"threshold={learning.get('previous_threshold')}->{learning.get('updated_threshold')}",
        )

    tasks = client.get("/writeback/tasks")
    task_rows = tasks.json().get("tasks", []) if tasks.status_code == 200 else []
    _record(
        checks,
        "writeback_task_created",
        tasks.status_code == 200 and bool(task_rows),
        f"tasks={len(task_rows)}",
    )


def _check_critic_rejection(checks: list[EvalCheck], body: dict[str, Any]) -> None:
    decision = body.get("decision") if isinstance(body.get("decision"), dict) else {}
    action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
    _record(
        checks,
        "critic_rejection_downgrades",
        decision.get("status") == "rejected"
        and decision.get("critic_verdict") == "rejected"
        and action.get("type") == "monitor",
        (
            f"status={decision.get('status')} verdict={decision.get('critic_verdict')} "
            f"action={action.get('type')}"
        ),
    )


def _check_product_catalog(checks: list[EvalCheck], client: TestClient) -> None:
    attention = client.get("/products/attention?limit=2")
    _record(
        checks,
        "product_attention_http",
        attention.status_code == 200,
        _status_detail(attention),
    )
    if attention.status_code == 200:
        body = attention.json()
        items = body.get("items") if isinstance(body.get("items"), list) else []
        totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
        _record(
            checks,
            "product_attention_bounded",
            len(items) <= 2
            and int(totals.get("sell_first_products") or 0) >= 1
            and all(item.get("requires_attention") is True for item in items),
            (
                f"items={len(items)} "
                f"sell_first_products={totals.get('sell_first_products')}"
            ),
        )

    summary = client.get("/data/seed/summary").json().get("seed_data", {})
    query = str(summary.get("product_name") or "")
    search = client.get("/products/search", params={"q": query, "limit": 3})
    _record(checks, "product_search_http", search.status_code == 200, _status_detail(search))
    if search.status_code == 200:
        body = search.json()
        products = body.get("products") if isinstance(body.get("products"), list) else []
        first = products[0] if products and isinstance(products[0], dict) else {}
        lots = first.get("fefo_batches") if isinstance(first.get("fefo_batches"), list) else []
        first_lot = lots[0] if lots and isinstance(lots[0], dict) else {}
        _record(
            checks,
            "product_search_attention_ranked",
            len(products) <= 3
            and bool(first)
            and first.get("source") == "generated_world"
            and first.get("name") == query,
            (
                f"first={first.get('name')} sell_first_units={first.get('sell_first_units')} "
                f"lot_count={first.get('lot_count')} first_lot={first_lot.get('lot')}"
            ),
        )
        source_counts = (
            body.get("source_counts") if isinstance(body.get("source_counts"), dict) else {}
        )
        scanned = int(source_counts.get("synthetic_scanned") or 0)
        budget = int(source_counts.get("synthetic_scan_budget") or 0)
        _record(
            checks,
            "product_search_scan_bounded",
            scanned == 0 and budget == 0,
            f"scanned={scanned} budget={budget}",
        )


def _check_store_intelligence_tools(checks: list[EvalCheck], client: TestClient) -> None:
    fefo = client.post(
        "/intelligence/stock/fefo-split",
        json={
            "sku": "milk_2l",
            "as_of": "2026-07-06",
            "batches": [
                {
                    "sku": "milk_2l",
                    "lot": "MILK-OLD-0707",
                    "units": 10,
                    "expiry_date": "2026-07-07",
                    "received_date": "2026-07-03",
                    "location": "fridge_a",
                },
                {
                    "sku": "milk_2l",
                    "lot": "MILK-NEW-0713",
                    "units": 20,
                    "expiry_date": "2026-07-13",
                    "received_date": "2026-07-06",
                    "location": "fridge_a",
                },
            ],
        },
    )
    _record(checks, "fefo_split_http", fefo.status_code == 200, _status_detail(fefo))
    if fefo.status_code == 200:
        split = fefo.json().get("batch_split", {})
        batches = split.get("fefo_batches") if isinstance(split.get("fefo_batches"), list) else []
        first_lot = batches[0] if batches and isinstance(batches[0], dict) else {}
        _record(
            checks,
            "fefo_split_batch_math",
            split.get("total_units") == 30
            and split.get("priority_sell_units") == 10
            and split.get("normal_units") == 20
            and first_lot.get("lot") == "MILK-OLD-0707",
            (
                f"priority={split.get('priority_sell_units')} "
                f"normal={split.get('normal_units')} first_lot={first_lot.get('lot')}"
            ),
        )

    delivery = client.post(
        "/intelligence/deliveries/reconcile",
        json={
            "sku": "milk_2l",
            "ordered_units": 50,
            "asn_units": 50,
            "received_units": 38,
            "accepted_units": 32,
            "short_dated_units": 6,
        },
    )
    _record(
        checks,
        "delivery_reconcile_http",
        delivery.status_code == 200,
        _status_detail(delivery),
    )
    if delivery.status_code == 200:
        reconciliation = delivery.json().get("delivery_reconciliation", {})
        _record(
            checks,
            "delivery_reconcile_exception_math",
            reconciliation.get("status") == "exception"
            and reconciliation.get("missing_units") == 12
            and reconciliation.get("supplier_fill_rate") == "0.76",
            (
                f"status={reconciliation.get('status')} "
                f"missing_units={reconciliation.get('missing_units')} "
                f"fill_rate={reconciliation.get('supplier_fill_rate')}"
            ),
        )

    supplier = client.post(
        "/intelligence/suppliers/cover-plan",
        json={
            "sku": "milk_2l",
            "units_on_hand": 12,
            "forecast_daily_units": "10",
            "supplier_lead_time_days": "3",
            "transfer_available_units": 18,
        },
    )
    _record(
        checks,
        "supplier_cover_http",
        supplier.status_code == 200,
        _status_detail(supplier),
    )
    if supplier.status_code == 200:
        cover = supplier.json().get("supplier_cover", {})
        _record(
            checks,
            "supplier_cover_action_math",
            cover.get("recommended_action") == "transfer"
            and cover.get("gap_before_delivery_units") == 18
            and cover.get("transfer_units_recommended") == 18,
            (
                f"action={cover.get('recommended_action')} "
                f"gap={cover.get('gap_before_delivery_units')} "
                f"transfer={cover.get('transfer_units_recommended')}"
            ),
        )

    outcome = client.post(
        "/intelligence/outcomes/summarize",
        json={
            "sku": "yoghurt_1l",
            "action": "markdown",
            "predicted_sell_through_units": 24,
            "actual_sell_through_units": 30,
            "predicted_waste_units": 8,
            "actual_waste_units": 5,
        },
    )
    _record(
        checks,
        "outcome_summary_http",
        outcome.status_code == 200,
        _status_detail(outcome),
    )
    if outcome.status_code == 200:
        learning = outcome.json().get("learning_summary", {})
        _record(
            checks,
            "outcome_summary_learning_math",
            learning.get("sell_through_delta_units") == 6
            and learning.get("waste_delta_units") == -3
            and learning.get("score") == "0.72",
            (
                f"sell_through_delta={learning.get('sell_through_delta_units')} "
                f"waste_delta={learning.get('waste_delta_units')} "
                f"score={learning.get('score')}"
            ),
        )


def _record(checks: list[EvalCheck], name: str, passed: bool, detail: str) -> None:
    checks.append(EvalCheck(name=name, passed=bool(passed), detail=detail))


def _check_inference_and_submission(checks: list[EvalCheck], client: TestClient) -> None:
    inference = client.get("/inference/readiness")
    _record(
        checks,
        "inference_readiness_http",
        inference.status_code == 200,
        _status_detail(inference),
    )
    if inference.status_code == 200:
        body = inference.json()
        details = body.get("inference") if isinstance(body.get("inference"), dict) else body
        timeout = float(details.get("timeout_seconds") or 0)
        _record(
            checks,
            "inference_timeout_submission_safe",
            0 < timeout < 30,
            f"timeout={timeout}",
        )

    submission = client.get("/submission/readiness")
    _record(
        checks,
        "submission_readiness_http",
        submission.status_code == 200,
        _status_detail(submission),
    )
    if submission.status_code == 200:
        body = submission.json()
        submission_checks = body.get("checks") if isinstance(body.get("checks"), dict) else {}
        _record(
            checks,
            "submission_track_three",
            body.get("track") == "Track 3: Unicorn"
            and submission_checks.get("docker_image_required") == "no",
            (
                f"track={body.get('track')} "
                f"docker={submission_checks.get('docker_image_required')}"
            ),
        )


def _status_detail(response: Any) -> str:
    return f"status_code={getattr(response, 'status_code', 'missing')}"


def _write_headers() -> dict[str, str]:
    api_key = os.getenv("API_KEY", "")
    return {"x-api-key": api_key} if api_key else {}


def _reset_in_memory_demo_state() -> None:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    bus_backend = os.getenv("SHELFWISE_BUS_BACKEND", "memory").strip().lower()
    if backend != "memory" or bus_backend != "memory":
        return
    for store in (
        decision_store,
        learning_store,
        event_store,
        journal,
        trace_registry,
        tool_audit,
        model_run_registry,
        prompt_registry,
        tenant_fact_store,
        writeback_sink,
        worldgen_run_store,
        world_snapshot_store,
        cold_chain_demo,
    ):
        clear = getattr(store, "clear", None)
        if callable(clear):
            clear()
    clear_bus = getattr(event_bus, "clear", None)
    if callable(clear_bus):
        clear_bus()
    write_limiter.configure(capacity=240, refill_per_s=8.0, max_keys=1024)
