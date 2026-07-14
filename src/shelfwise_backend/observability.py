from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from .workload import build_workload_snapshot


def build_observability_snapshot(
    *,
    tenant_id: str,
    data_domain: str | None = None,
    decisions: list[dict[str, Any]],
    model_runs: list[dict[str, Any]],
    inbound_records: list[dict[str, Any]],
    events: list[dict[str, Any]],
    bus_stats: dict[str, Any],
    writeback_tasks: list[dict[str, Any]],
    worker_status: dict[str, Any],
    worker_runs: list[dict[str, Any]],
    learning_events: list[dict[str, Any]],
    tenant_facts: list[dict[str, Any]],
    rate_zar_per_1k: Decimal,
    candidate_records: list[dict[str, Any]] | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant_decisions = [
        decision
        for decision in decisions
        if str(decision.get("tenant_id") or "default") == tenant_id
    ]
    decision_ids = {str(decision.get("id")) for decision in tenant_decisions}
    tenant_events = [
        event for event in events if str(event.get("tenant_id") or "default") == tenant_id
    ]
    tenant_learning_events = [
        event
        for event in learning_events
        if str(event.get("decision_id") or "") in decision_ids
    ]
    return {
        "tenant_id": tenant_id,
        "data_domain": data_domain,
        "decisions": _decision_metrics(tenant_decisions),
        "inference": _inference_metrics(model_runs, rate_zar_per_1k=rate_zar_per_1k),
        "connectors": _connector_metrics(inbound_records),
        "events": _event_metrics(tenant_events, bus_stats),
        "writeback": _writeback_metrics(writeback_tasks),
        "worker": _worker_metrics(worker_status, worker_runs, tenant_id=tenant_id),
        "learning": _learning_metrics(tenant_learning_events, tenant_facts),
        "candidates": _candidate_metrics(candidate_records or []),
        "open_orders": _open_order_metrics(open_orders or []),
        "hitl_workload": build_workload_snapshot(
            tenant_decisions,
            candidates=candidate_records or [],
            now=now,
        ),
    }


def _decision_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(decision.get("status") or "unknown") for decision in decisions)
    roles = Counter(str(decision.get("role") or "unknown") for decision in decisions)
    critic = Counter(str(decision.get("critic_verdict") or "unknown") for decision in decisions)
    resolved = statuses["approved"] + statuses["rejected"]
    total = len(decisions)
    recovered = _recovered_money(decisions)
    return {
        "total": total,
        "status_counts": dict(sorted(statuses.items())),
        "role_counts": dict(sorted(roles.items())),
        "critic_counts": dict(sorted(critic.items())),
        "pending": statuses["pending"],
        "approved": statuses["approved"],
        "rejected": statuses["rejected"],
        "hitl_resolution_rate": _rate(resolved, total),
        "critic_rejection_rate": _rate(critic["rejected"], total),
        "recovered": recovered.to_dict(),
    }


def _inference_metrics(
    model_runs: list[dict[str, Any]],
    *,
    rate_zar_per_1k: Decimal,
) -> dict[str, Any]:
    status_counts = Counter(str(run.get("status") or "unknown") for run in model_runs)
    provider_counts = Counter(str(run.get("provider") or "unknown") for run in model_runs)
    model_counts = Counter(str(run.get("model") or "unknown") for run in model_runs)
    input_tokens = sum(_int(run.get("input_tokens")) for run in model_runs)
    output_tokens = sum(_int(run.get("output_tokens")) for run in model_runs)
    latency_values = [_int(run.get("latency_ms")) for run in model_runs]
    total_tokens = input_tokens + output_tokens
    return {
        "model_runs": len(model_runs),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": _cost_money(total_tokens, rate_zar_per_1k).to_dict(),
        "avg_latency_ms": _average(latency_values),
        "status_counts": dict(sorted(status_counts.items())),
        "provider_counts": dict(sorted(provider_counts.items())),
        "model_counts": dict(sorted(model_counts.items())),
    }


def _connector_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    systems = Counter(str(record.get("source_system") or "unknown") for record in records)
    invalid = [
        record
        for record in records
        if not ((record.get("validation") or {}).get("ok", True))
    ]
    source_qualities = [
        _decimal(record.get("source_quality"), default=Decimal("1")) for record in records
    ]
    return {
        "inbound_records": len(records),
        "invalid_records": len(invalid),
        "invalid_rate": _rate(len(invalid), len(records)),
        "by_system": dict(sorted(systems.items())),
        "avg_source_quality": str(_decimal_average(source_qualities)),
    }


def _event_metrics(events: list[dict[str, Any]], bus_stats: dict[str, Any]) -> dict[str, Any]:
    event_types = Counter(str(event.get("type") or "unknown") for event in events)
    return {
        "stored_events": len(events),
        "by_type": dict(sorted(event_types.items())),
        "bus": bus_stats,
    }


def _writeback_metrics(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(task.get("status") or "unknown") for task in tasks)
    return {
        "tasks": len(tasks),
        "status_counts": dict(sorted(statuses.items())),
        "pending_external_write": statuses["pending_external_write"],
    }


def _worker_metrics(
    worker_status: dict[str, Any],
    worker_runs: list[dict[str, Any]],
    *,
    tenant_id: str,
) -> dict[str, Any]:
    tenant_runs = [
        run for run in worker_runs if str(run.get("tenant_id") or "default") == tenant_id
    ]
    statuses = Counter(str(run.get("status") or "unknown") for run in tenant_runs)
    return {
        "service": worker_status,
        "runs": len(tenant_runs),
        "status_counts": dict(sorted(statuses.items())),
        "failed_runs": statuses["failed"],
        "done_runs": statuses["done"],
    }


def _learning_metrics(
    learning_events: list[dict[str, Any]],
    tenant_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    active_facts = [fact for fact in tenant_facts if fact.get("active", True)]
    return {
        "learning_events": len(learning_events),
        "tenant_facts": len(tenant_facts),
        "active_facts": len(active_facts),
        "tombstoned_facts": len(tenant_facts) - len(active_facts),
    }


def _candidate_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize candidate volume and lifecycle state for queue health dashboards."""
    statuses = Counter(str(record.get("status") or "unknown") for record in records)
    types = Counter(str(record.get("candidate_type") or "unknown") for record in records)
    return {
        "total": len(records),
        "status_counts": dict(sorted(statuses.items())),
        "type_counts": dict(sorted(types.items())),
        "monitoring_only": sum(1 for record in records if record.get("monitoring_only")),
        "suppressed": statuses["suppressed"],
        "pending": statuses["pending"],
    }


def _open_order_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize committed supplier units that can prevent reorder noise."""
    statuses = Counter(str(record.get("status") or "unknown") for record in records)
    return {
        "total": len(records),
        "status_counts": dict(sorted(statuses.items())),
        "open": statuses["open"],
        "remaining_units": sum(_int(record.get("remaining_units")) for record in records),
    }


def _recovered_money(decisions: list[dict[str, Any]]) -> Money:
    total = Decimal("0")
    for decision in decisions:
        if decision.get("status") != "approved":
            continue
        outcome = decision.get("outcome") if isinstance(decision.get("outcome"), dict) else {}
        recovered = (
            outcome.get("rand_recovered")
            if isinstance(outcome.get("rand_recovered"), dict)
            else {}
        )
        total += Decimal(str(recovered.get("minor_units") or 0))
    return Money(minor_units=int(total), currency="ZAR")


def _cost_money(total_tokens: int, rate_zar_per_1k: Decimal) -> Money:
    return Money.zar(Decimal(total_tokens) / Decimal("1000") * rate_zar_per_1k)


def _rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0000"
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001")))


def _average(values: list[int]) -> int:
    if not values:
        return 0
    return int(sum(values) / len(values))


def _decimal_average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0.00")
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.01"))


def _decimal(value: object, *, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return default


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
