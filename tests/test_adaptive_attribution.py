from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from shelfwise_backend.adaptive_attribution import (
    AttributionConfig,
    build_attributed_trace,
    extract_representation,
)
from shelfwise_backend.ingest_pipeline import record_cascade
from shelfwise_backend.model_runs import (
    record_agentic_execution_failure,
    record_model_run,
)
from shelfwise_backend.state import model_run_registry, trace_registry
from shelfwise_backend.trace import CascadeTrace, TraceRegistry
from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_eval.full_system import FullSystemConfig, run_full_system


def _cascade(
    correlation_id: str,
    *,
    tenant_id: str = "tenant-a",
    data_domain: str = "world_simulation",
    extra_span: bool = False,
) -> dict:
    trace = [
        {
            "name": "decision_science.forecast_demand",
            "status": "ok",
            "ms": 10,
            "detail": {"daily_units": "12"},
        }
    ]
    if extra_span:
        trace.append(
            {
                "name": "unexpected.parallel_step",
                "status": "error",
                "ms": 900,
                "detail": {"retry": True},
            }
        )
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "data_domain": data_domain,
        "scenario": "stage4_loadshedding_x_payday_yoghurt",
        "status": "ok",
        "trace": trace,
        "evidence": [
            {
                "agent": "demand",
                "conclusion": "Forecast is supported by the measured sales window.",
                "supporting_data": [{"fact": "daily_units", "value": "12"}],
                "confidence": "0.90",
                "sources": [{"kind": "dataset", "ref": "world"}],
                "requires_human_review": False,
            }
        ],
        "decision": {
            "id": f"decision-{correlation_id}",
            "status": "pending",
            "action": {
                "type": "monitor",
                "params": {"sku": "SKU-1"},
                "risk_tier": "low",
            },
            "summary": "Monitor the verified demand signal.",
            "critic_verdict": "approved",
        },
    }


def _agentic_cascade(correlation_id: str) -> dict:
    result = _cascade(correlation_id)
    result["agentic"] = True
    result["model_calls"] = [
        {
            "call_id": f"model-{correlation_id}",
            "role": "critic",
            "provider": "vllm_mi300x",
            "model": "google/gemma-4-E4B-it",
            "endpoint_host": "inference.example",
            "used_network": True,
            "usage": {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240},
            "latency_ms": 120,
            "correlation_id": correlation_id,
            "finish_reason": "stop",
            "status": "ok",
            "fallback": False,
        }
    ]
    result["tool_calls"] = [
        {
            "role": "tool",
            "tool_call_id": f"tool-{correlation_id}",
            "name": "get_stock",
            "content": '{"units_on_hand":240}',
        }
    ]
    return result


def test_disabled_flag_preserves_original_trace_contract(monkeypatch) -> None:
    monkeypatch.delenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", raising=False)
    result = record_cascade(_cascade("disabled"))

    assert "adaptive_attribution" not in result
    trace = trace_registry.get(
        "disabled",
        tenant_id="tenant-a",
        data_domain="world_simulation",
    )
    assert trace is not None
    assert set(trace) == {
        "correlation_id",
        "tenant_id",
        "data_domain",
        "scenario",
        "status",
        "spans",
        "evidence_agents",
        "decision_id",
    }
    record_model_run(
        {
            "id": "disabled-model-failure",
            "tenant_id": "tenant-a",
            "correlation_id": "disabled-failed-run",
            "agent": "critic",
            "model": "offline",
            "provider": "offline",
            "prompt_version": "prompt-v1",
            "schema_version": "critic_verdict",
            "input_tokens": 1,
            "output_tokens": 0,
            "latency_ms": 1,
            "status": "error",
            "error_detail": "recorded only in the existing model-run registry",
        }
    )
    assert model_run_registry.list(tenant_id="tenant-a")[-1].status == "error"
    assert trace_registry.get(
        "disabled-failed-run",
        tenant_id="tenant-a",
        data_domain="world_simulation",
    ) is None


def test_invalid_optional_settings_cannot_break_legacy_cascade_recording(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_ATTRIBUTION_MIN_SUCCESSES", "invalid")

    result = record_cascade(_cascade("invalid-settings"))

    assert "adaptive_attribution" not in result
    assert trace_registry.get(
        "invalid-settings",
        tenant_id="tenant-a",
        data_domain="world_simulation",
    ) is not None


def test_verified_successes_calibrate_and_locate_a_deviation() -> None:
    config = AttributionConfig(min_successes=2, alpha=0.05)
    first = build_attributed_trace(_cascade("first"), [], config=config)
    second = build_attributed_trace(
        _cascade("second"),
        [first.representation],
        config=config,
    )
    deviant = build_attributed_trace(
        _cascade("deviant", extra_span=True),
        [first.representation, second.representation],
        config=config,
    )

    assert first.attribution["state"] == "warming_up"
    assert second.attribution["state"] == "warming_up"
    assert deviant.attribution["state"] == "suspicious"
    assert deviant.attribution["anomaly_score"] > deviant.attribution["threshold"]
    assert deviant.attribution["suspected_step"]["name"] == "unexpected.parallel_step"
    assert deviant.attribution["controlled_replay"] == {
        "recommended": True,
        "automatic": False,
        "requires_human_approval": True,
        "correlation_id": "deviant",
        "decision_id": "decision-deviant",
    }
    assert not deviant.verified_success


def test_reference_reader_isolates_tenant_domain_and_family() -> None:
    registry = TraceRegistry(max_items=10)
    representation = extract_representation(_cascade("source"))
    for tenant_id, data_domain, family in (
        ("tenant-a", "world_simulation", "cascade:golden"),
        ("tenant-b", "world_simulation", "cascade:golden"),
        ("tenant-a", "operational_twin", "cascade:golden"),
        ("tenant-a", "world_simulation", "cascade:expiry"),
    ):
        registry.put(
            CascadeTrace(
                correlation_id=f"{tenant_id}-{data_domain}-{family}",
                tenant_id=tenant_id,
                data_domain=data_domain,
                scenario="scenario",
                trajectory_family=family,
                verified_success=True,
                representation=deepcopy(representation),
            )
        )

    references = registry.successful_representations(
        tenant_id="tenant-a",
        data_domain="world_simulation",
        trajectory_family="cascade:golden",
    )

    assert references == [representation]


def test_failed_model_run_becomes_safe_attributed_trace_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_ATTRIBUTION_MIN_SUCCESSES", "2")
    config = AttributionConfig(min_successes=2, alpha=0.05)
    for correlation_id in ("baseline-1", "baseline-2"):
        baseline = build_attributed_trace(
            _agentic_cascade(correlation_id),
            [],
            config=config,
        )
        trace_registry.put(baseline)

    record_model_run(
        {
            "id": "model-failure",
            "tenant_id": "tenant-a",
            "correlation_id": "failed-agentic-run",
            "agent": "critic",
            "model": "google/gemma-4-E4B-it",
            "provider": "vllm_mi300x",
            "prompt_version": "prompt-v1",
            "schema_version": "critic_verdict",
            "input_tokens": 200,
            "output_tokens": 0,
            "latency_ms": 1200,
            "status": "error",
            "error_detail": "secret upstream diagnostic that must not enter the trace",
        }
    )

    trace = trace_registry.get(
        "failed-agentic-run",
        tenant_id="tenant-a",
        data_domain="world_simulation",
    )
    assert trace is not None
    assert trace["status"] == "model_run_error"
    assert trace["adaptive_attribution"]["state"] == "suspicious"
    assert trace["adaptive_attribution"]["verification_failures"] == ["model_run_error"]
    assert "secret upstream diagnostic" not in str(trace)


def test_post_model_orchestration_failure_is_captured_without_raw_exception(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", "true")
    event = Event(
        id="failed-event",
        type=EventType.SCAN,
        ts=datetime.now(UTC),
        actor="test",
        payload={},
        source=EventSource.API,
        tenant_id="tenant-a",
        data_domain="world_simulation",
        correlation_id="failed-after-model",
    )

    record_agentic_execution_failure(
        event,
        failure_code="ungrounded_answer",
    )

    trace = trace_registry.get(
        "failed-after-model",
        tenant_id="tenant-a",
        data_domain="world_simulation",
    )
    assert trace is not None
    assert trace["status"] == "ungrounded_answer"
    assert trace["adaptive_attribution"]["state"] == "failed_unscored"
    assert trace["adaptive_attribution"]["verification_failures"] == [
        "ungrounded_answer"
    ]
    assert trace["adaptive_attribution"]["controlled_replay"]["recommended"] is True


def test_attribution_never_auto_replays_or_trains_operational_data() -> None:
    config = AttributionConfig(min_successes=2, alpha=0.05)
    reference = build_attributed_trace(_cascade("reference"), [], config=config)
    references = [reference.representation, deepcopy(reference.representation)]
    world = build_attributed_trace(_cascade("world"), references, config=config)
    operational = build_attributed_trace(
        _cascade("operational", data_domain="operational_twin"),
        references,
        config=config,
    )

    assert world.attribution["state"] == "normal"
    assert world.attribution["future_training"]["classification"] == "review_candidate"
    assert world.attribution["future_training"]["automatic"] is False
    assert operational.attribution["future_training"] == {
        "classification": "ineligible",
        "reason": "operational_twin_data_is_never_an_automatic_training_source",
        "automatic": False,
    }
    assert operational.attribution["controlled_replay"]["automatic"] is False


def test_full_system_harness_exercises_enabled_attribution(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_ATTRIBUTION_MIN_SUCCESSES", "2")

    report = run_full_system(
        FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            assortment_sizes=(None,),
            chat_every_n_cycles=1,
            run_id="adaptive-attribution-harness",
        )
    )

    traces = trace_registry.list(tenant_id="local", data_domain="world_simulation")
    assert report.passed is True, report.failures
    assert traces
    assert all("adaptive_attribution" in trace for trace in traces)
    assert any(
        trace["adaptive_attribution"]["reference_successes"] > 0
        for trace in traces
    )
