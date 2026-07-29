"""Derive bounded failure-attribution evidence from existing ShelfWise receipts.

This is an OAT-inspired structured one-class monitor, not a reproduction of the paper's
neural-CDE implementation. ShelfWise already has typed spans, evidence, model/tool telemetry,
and a bounded trace registry; this module compares those existing receipts without storing
prompts, raw tool payloads, or a parallel trajectory log.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from math import ceil
from statistics import median
from typing import Any

from .trace import CascadeTrace, trace_from_cascade

_TRUE_VALUES = {"1", "true", "yes", "on"}
_OK_STATUSES = {"ok", "success"}
_SUPPORTED_DECISION_STATUSES = {"pending", "approved", "rejected"}
_MAX_STEPS = 64
_THRESHOLD_FLOOR = 0.35
_VERSION = "shelfwise-structured-oat-v1"


@dataclass(frozen=True, slots=True)
class AttributionConfig:
    """Validated runtime settings for one attribution calculation."""

    min_successes: int = 5
    alpha: float = 0.05

    def __post_init__(self) -> None:
        if not 2 <= self.min_successes <= 200:
            raise ValueError("SHELFWISE_ATTRIBUTION_MIN_SUCCESSES must be between 2 and 200")
        if not 0.001 <= self.alpha <= 0.25:
            raise ValueError("SHELFWISE_ATTRIBUTION_ALPHA must be between 0.001 and 0.25")


def adaptive_attribution_enabled() -> bool:
    """Return whether the incremental trace extension is explicitly enabled."""

    return (
        os.getenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", "")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def load_attribution_config() -> AttributionConfig:
    """Load bounded calibration settings, failing closed on invalid values."""

    try:
        min_successes = int(os.getenv("SHELFWISE_ATTRIBUTION_MIN_SUCCESSES", "5"))
        alpha = float(os.getenv("SHELFWISE_ATTRIBUTION_ALPHA", "0.05"))
    except ValueError as exc:
        raise ValueError("adaptive-attribution settings must be numeric") from exc
    return AttributionConfig(min_successes=min_successes, alpha=alpha)


def build_attributed_trace(
    cascade: Mapping[str, Any],
    successful_representations: Sequence[Sequence[Mapping[str, Any]]],
    *,
    config: AttributionConfig | None = None,
) -> CascadeTrace:
    """Build the existing trace record plus optional one-class deviation evidence."""

    effective = config or load_attribution_config()
    payload = deepcopy(dict(cascade))
    trace = trace_from_cascade(payload)
    representation = extract_representation(payload)
    family = trajectory_family(payload)
    structurally_verified, verification_failures = verify_success(payload)
    public, suspicious = _score_attribution(
        trace=trace,
        representation=representation,
        references=successful_representations,
        config=effective,
        structurally_verified=structurally_verified,
        verification_failures=verification_failures,
        family=family,
    )
    trace.attribution = public
    trace.trajectory_family = family
    trace.verified_success = structurally_verified and not suspicious
    trace.representation = representation
    return trace


def build_failed_model_trace(
    run: Mapping[str, Any],
    successful_representations: Sequence[Sequence[Mapping[str, Any]]],
    *,
    config: AttributionConfig | None = None,
) -> CascadeTrace:
    """Create a safe failed trajectory from an existing persisted ModelRun receipt."""

    tenant_id = str(run.get("tenant_id") or "").strip()
    correlation_id = str(run.get("correlation_id") or "").strip()
    data_domain = str(run.get("data_domain") or "world_simulation").strip()
    family = trajectory_family_for_model_run(run)
    return build_failed_execution_trace(
        [run],
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        data_domain=data_domain,
        trajectory_family=family,
        failure_code="model_run_error",
        successful_representations=successful_representations,
        config=config,
    )


def build_failed_execution_trace(
    model_runs: Sequence[Mapping[str, Any]],
    *,
    correlation_id: str,
    tenant_id: str,
    data_domain: str,
    trajectory_family: str,
    failure_code: str,
    successful_representations: Sequence[Sequence[Mapping[str, Any]]],
    config: AttributionConfig | None = None,
) -> CascadeTrace:
    """Build a failed trace for provider, tool, schema, grounding, or deadline errors."""

    if not tenant_id.strip() or not correlation_id.strip():
        raise ValueError("failed execution trace requires tenant_id and correlation_id")
    safe_failure_code = _bounded_label(failure_code).lower() or "agentic_execution_error"
    representation = [_model_run_step(run) for run in model_runs][:_MAX_STEPS - 1]
    representation.append(
        {
            **_step(
                "verification",
                safe_failure_code,
                "error",
            ),
            "position": len(representation),
        }
    )
    trace = CascadeTrace(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        data_domain=data_domain,
        scenario="agentic_execution_failure",
        spans=[],
        evidence_agents=list(
            dict.fromkeys(str(run.get("agent") or "unknown") for run in model_runs)
        ),
        status=safe_failure_code,
    )
    public, _ = _score_attribution(
        trace=trace,
        representation=representation,
        references=successful_representations,
        config=config or load_attribution_config(),
        structurally_verified=False,
        verification_failures=(safe_failure_code,),
        family=trajectory_family,
    )
    trace.attribution = public
    trace.trajectory_family = trajectory_family
    trace.representation = representation
    return trace


def verify_success(cascade: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Verify execution success from server-owned receipts, never from prose."""

    failures: list[str] = []
    if str(cascade.get("status") or "ok").lower() not in _OK_STATUSES:
        failures.append("cascade_status")
    decision = cascade.get("decision")
    if not isinstance(decision, Mapping) or not str(decision.get("id") or "").strip():
        failures.append("decision_receipt")
    else:
        if str(decision.get("status") or "").lower() not in _SUPPORTED_DECISION_STATUSES:
            failures.append("decision_status")
        action = decision.get("action")
        if not isinstance(action, Mapping) or not str(action.get("type") or "").strip():
            failures.append("decision_action")
    evidence = cascade.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        failures.append("evidence_receipts")
    elif any(not _valid_evidence(item) for item in evidence):
        failures.append("evidence_contract")
    if any(_step_status(item) not in _OK_STATUSES for item in _items(cascade.get("trace"))):
        failures.append("trace_span_status")
    model_calls = _items(cascade.get("model_calls"))
    if any(_step_status(item) not in _OK_STATUSES for item in model_calls):
        failures.append("model_call_status")
    if cascade.get("agentic") is True:
        if not model_calls:
            failures.append("agentic_model_receipts")
        if not _items(cascade.get("tool_calls")):
            failures.append("agentic_tool_receipts")
    return not failures, tuple(dict.fromkeys(failures))


def extract_representation(cascade: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract non-sensitive structural steps from existing cascade receipts."""

    steps: list[dict[str, Any]] = []
    for span in _items(cascade.get("trace")):
        steps.append(
            _step(
                "trace_span",
                span.get("name"),
                _step_status(span),
                metrics={
                    "latency_ms": _non_negative(span.get("ms")),
                    "detail_fields": len(span.get("detail") or {})
                    if isinstance(span.get("detail"), Mapping)
                    else 0,
                },
            )
        )
    for call in _items(cascade.get("model_calls")):
        usage = call.get("usage") if isinstance(call.get("usage"), Mapping) else {}
        steps.append(
            _step(
                "model_call",
                call.get("role"),
                _step_status(call),
                metrics={
                    "latency_ms": _non_negative(call.get("latency_ms")),
                    "input_tokens": _non_negative(usage.get("input_tokens")),
                    "output_tokens": _non_negative(usage.get("output_tokens")),
                },
                attributes={
                    "provider": _bounded_label(call.get("provider")),
                    "model": _bounded_label(call.get("model")),
                    "used_network": bool(call.get("used_network")),
                    "fallback": bool(call.get("fallback")),
                },
            )
        )
    for call in _items(cascade.get("tool_calls")):
        content = call.get("content")
        steps.append(
            _step(
                "tool_call",
                call.get("name"),
                "ok",
                metrics={
                    "result_bytes": (
                        len(content.encode("utf-8")) if isinstance(content, str) else 0
                    )
                },
            )
        )
    for item in _items(cascade.get("evidence")):
        steps.append(
            _step(
                "evidence",
                item.get("agent"),
                "ok",
                metrics={
                    "supporting_facts": len(_items(item.get("supporting_data"))),
                    "sources": len(_items(item.get("sources"))),
                    "confidence_milli": _confidence_milli(item.get("confidence")),
                },
                attributes={"requires_human_review": bool(item.get("requires_human_review"))},
            )
        )
    decision = cascade.get("decision")
    if isinstance(decision, Mapping):
        action = decision.get("action") if isinstance(decision.get("action"), Mapping) else {}
        steps.append(
            _step(
                "decision",
                action.get("type"),
                _step_status(decision),
                attributes={
                    "risk_tier": _bounded_label(action.get("risk_tier")),
                    "critic_verdict": _bounded_label(decision.get("critic_verdict")),
                },
            )
        )
    return [{**item, "position": index} for index, item in enumerate(steps[:_MAX_STEPS])]


def trajectory_family(cascade: Mapping[str, Any]) -> str:
    """Return a stable family shared by equivalent successful and failed runs."""

    scenario = str(cascade.get("scenario") or "unknown").lower()
    prefix = (
        "agentic"
        if cascade.get("agentic") is True or cascade.get("model_calls")
        else "cascade"
    )
    return f"{prefix}:{_scenario_slug(scenario)}"


def trajectory_family_for_model_run(run: Mapping[str, Any]) -> str:
    """Map an existing schema-version receipt back to its agentic cascade family."""

    schema = str(run.get("schema_version") or "").lower()
    prefixes = ("procurement", "sales", "catalog_price", "expiry", "cold_chain")
    family = next((prefix for prefix in prefixes if schema.startswith(f"{prefix}_")), "golden")
    return f"agentic:{family}"


def trajectory_family_for_event(event: Mapping[str, Any]) -> str:
    """Derive an agentic family when failure occurs before a ModelRun exists."""

    event_type = str(event.get("type") or "").lower()
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    if event_type == "supplier_update":
        family = "procurement"
    elif event_type == "expiry_entry":
        family = "expiry"
    elif event_type == "cold_chain_alert":
        family = "cold_chain"
    elif event_type == "sale" and {
        "unit_price_cents",
        "catalog_price_cents",
    } <= payload.keys():
        family = "catalog_price"
    elif event_type == "sale":
        family = "sales"
    else:
        family = "golden"
    return f"agentic:{family}"


def _score_attribution(
    *,
    trace: CascadeTrace,
    representation: list[dict[str, Any]],
    references: Sequence[Sequence[Mapping[str, Any]]],
    config: AttributionConfig,
    structurally_verified: bool,
    verification_failures: Sequence[str],
    family: str,
) -> tuple[dict[str, Any], bool]:
    normalized_refs = [
        [deepcopy(dict(step)) for step in reference[:_MAX_STEPS]]
        for reference in references
        if reference
    ]
    enough = len(normalized_refs) >= config.min_successes
    score: float | None = None
    threshold: float | None = None
    step_scores: list[dict[str, Any]] = []
    suspicious = False
    if enough:
        profile = _build_profile(normalized_refs)
        score, step_scores = _score_representation(representation, profile)
        calibration = sorted(
            _score_representation(reference, profile)[0] for reference in normalized_refs
        )
        threshold = max(_THRESHOLD_FLOOR, _quantile(calibration, 1.0 - config.alpha))
        suspicious = score > threshold
    if not structurally_verified:
        state = "suspicious" if suspicious else "failed" if enough else "failed_unscored"
    else:
        state = "suspicious" if suspicious else "normal" if enough else "warming_up"
    suspected_step = max(step_scores, key=lambda item: item["score"]) if step_scores else None
    training = _training_receipt(trace, structurally_verified, state)
    public = {
        "version": _VERSION,
        "state": state,
        "trajectory_family": family,
        "reference_successes": len(normalized_refs),
        "minimum_successes": config.min_successes,
        "structurally_verified_success": structurally_verified,
        "verification_failures": list(verification_failures),
        "anomaly_score": _rounded(score),
        "threshold": _rounded(threshold),
        "suspected_step": suspected_step,
        "step_scores": step_scores,
        "controlled_replay": {
            "recommended": suspicious or not structurally_verified,
            "automatic": False,
            "requires_human_approval": True,
            "correlation_id": trace.correlation_id,
            "decision_id": trace.decision_id,
        },
        "future_training": training,
    }
    return public, suspicious


def _build_profile(
    references: Sequence[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    width = max(len(reference) for reference in references)
    return [
        _profile_step([reference[index] for reference in references if index < len(reference)])
        for index in range(width)
    ]


def _profile_step(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, dict[str, float]] = {}
    metric_names = {name for step in steps for name in _mapping(step.get("metrics"))}
    for name in sorted(metric_names):
        values = [
            float(value)
            for step in steps
            if (value := _mapping(step.get("metrics")).get(name)) is not None
            and isinstance(value, int | float)
        ]
        if values:
            center = float(median(values))
            metrics[name] = {
                "center": center,
                "scale": max(
                    float(median(abs(value - center) for value in values)),
                    abs(center) * 0.1,
                    1.0,
                ),
            }
    attribute_names = {name for step in steps for name in _mapping(step.get("attributes"))}
    return {
        "kind": _mode(step.get("kind") for step in steps),
        "name": _mode(step.get("name") for step in steps),
        "status": _mode(step.get("status") for step in steps),
        "metrics": metrics,
        "attributes": {
            name: _mode(_mapping(step.get("attributes")).get(name) for step in steps)
            for name in sorted(attribute_names)
        },
    }


def _score_representation(
    representation: Sequence[Mapping[str, Any]],
    profile: Sequence[Mapping[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    width = max(len(representation), len(profile))
    for index in range(width):
        if index >= len(representation):
            rows.append(_public_step_score(index, None, 1.0, "missing_step"))
            continue
        step = representation[index]
        if index >= len(profile):
            rows.append(_public_step_score(index, step, 1.0, "unexpected_step"))
            continue
        score, reason = _score_step(step, profile[index])
        rows.append(_public_step_score(index, step, score, reason))
    overall = max((float(row["score"]) for row in rows), default=0.0)
    return overall, rows


def _score_step(step: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float, str]:
    components: list[float] = []
    reasons: list[str] = []
    for field in ("kind", "name", "status"):
        mismatch = step.get(field) != profile.get(field)
        components.append(1.0 if mismatch else 0.0)
        if mismatch:
            reasons.append(field)
    expected_attributes = _mapping(profile.get("attributes"))
    actual_attributes = _mapping(step.get("attributes"))
    for name, expected in expected_attributes.items():
        mismatch = actual_attributes.get(name) != expected
        components.append(1.0 if mismatch else 0.0)
        if mismatch:
            reasons.append(name)
    actual_metrics = _mapping(step.get("metrics"))
    for name, expected in _mapping(profile.get("metrics")).items():
        if not isinstance(expected, Mapping):
            continue
        actual = actual_metrics.get(name)
        if not isinstance(actual, int | float):
            components.append(1.0)
            reasons.append(name)
            continue
        center = float(expected.get("center") or 0.0)
        scale = max(float(expected.get("scale") or 1.0), 1.0)
        deviation = min(abs(float(actual) - center) / scale / 5.0, 1.0)
        components.append(deviation)
        if deviation >= 0.5:
            reasons.append(name)
    return sum(components) / max(len(components), 1), ",".join(reasons) or "within_profile"


def _model_run_step(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_step(
            "model_call",
            run.get("agent"),
            _step_status(run),
            metrics={
                "latency_ms": _non_negative(run.get("latency_ms")),
                "input_tokens": _non_negative(run.get("input_tokens")),
                "output_tokens": _non_negative(run.get("output_tokens")),
            },
            attributes={
                "provider": _bounded_label(run.get("provider")),
                "model": _bounded_label(run.get("model")),
                "used_network": True,
                "fallback": False,
            },
        ),
        "position": 0,
    }


def _training_receipt(
    trace: CascadeTrace,
    structurally_verified: bool,
    state: str,
) -> dict[str, Any]:
    if trace.data_domain != "world_simulation":
        return {
            "classification": "ineligible",
            "reason": "operational_twin_data_is_never_an_automatic_training_source",
            "automatic": False,
        }
    if not structurally_verified or state != "normal":
        return {
            "classification": "ineligible",
            "reason": "requires_a_verified_non_anomalous_simulation_trace",
            "automatic": False,
        }
    return {
        "classification": "review_candidate",
        "reason": "verified_simulation_trace_requires_dataset_review_and_redaction",
        "automatic": False,
    }


def _scenario_slug(scenario: str) -> str:
    if "procurement" in scenario or "reorder_supplier" in scenario:
        return "procurement"
    if "catalog" in scenario or "price_outlier" in scenario:
        return "catalog_price"
    if "pos_sale" in scenario or "price_integrity" in scenario:
        return "sales"
    if "expiry" in scenario:
        return "expiry"
    if "cold_chain" in scenario:
        return "cold_chain"
    if "stage4" in scenario or "golden" in scenario:
        return "golden"
    return _bounded_label(scenario) or "unknown"


def _valid_evidence(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    agent = str(value.get("agent") or "").strip()
    conclusion = str(value.get("conclusion") or "").strip()
    return bool(agent and conclusion)


def _step(
    kind: str,
    name: object,
    status: str,
    *,
    metrics: Mapping[str, int] | None = None,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": _bounded_label(name) or "unknown",
        "status": status,
        "metrics": dict(metrics or {}),
        "attributes": dict(attributes or {}),
    }


def _public_step_score(
    index: int,
    step: Mapping[str, Any] | None,
    score: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "position": index,
        "kind": str((step or {}).get("kind") or "missing"),
        "name": str((step or {}).get("name") or "missing"),
        "status": str((step or {}).get("status") or "missing"),
        "score": round(score, 6),
        "reason": reason,
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, ceil(probability * len(values)) - 1))
    return float(values[index])


def _mode(values: Sequence[object] | Any) -> object:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    counts = Counter(str(value) for value in cleaned)
    selected = min(counts, key=lambda value: (-counts[value], value))
    original = next(value for value in cleaned if str(value) == selected)
    return original


def _items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _step_status(value: Mapping[str, Any]) -> str:
    return str(value.get("status") or "ok").strip().lower()


def _non_negative(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _confidence_milli(value: object) -> int:
    try:
        return max(0, min(1000, int(float(value) * 1000)))
    except (TypeError, ValueError):
        return 0


def _bounded_label(value: object) -> str:
    return str(value or "").strip()[:120]


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


__all__ = [
    "AttributionConfig",
    "adaptive_attribution_enabled",
    "build_attributed_trace",
    "build_failed_execution_trace",
    "build_failed_model_trace",
    "extract_representation",
    "load_attribution_config",
    "trajectory_family",
    "trajectory_family_for_event",
    "trajectory_family_for_model_run",
    "verify_success",
]
