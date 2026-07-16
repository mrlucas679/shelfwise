from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from shelfwise_eval import full_system
from shelfwise_eval.full_system import (
    LIVE_REQUIRED_FEATURE_RECEIPTS,
    LIVE_REQUIRED_ROUTE_RECEIPTS,
    REQUIRED_FEATURE_RECEIPTS,
    REQUIRED_ROUTE_RECEIPTS,
    SCENARIO_ROTATION,
    FeatureReceipt,
    FullSystemConfig,
    RouteReceipt,
    audit_full_system_integrity,
    run_full_system,
)


def _passing_feature_receipts() -> list[FeatureReceipt]:
    return [
        FeatureReceipt(feature=feature, passed=True, detail="ok")
        for feature in REQUIRED_FEATURE_RECEIPTS
    ]


def _passing_route_receipts() -> list[RouteReceipt]:
    return [
        RouteReceipt(
            key=route,
            feature="test",
            status_code=200,
            ok=True,
            request_index=index,
        )
        for index, route in enumerate(REQUIRED_ROUTE_RECEIPTS, start=1)
    ]


def test_scenario_rotation_covers_every_required_operational_surface() -> None:
    assert SCENARIO_ROTATION == (
        "golden_expiry",
        "critic_rejection",
        "procurement",
        "sales",
        "misprice",
        "cold_chain",
        "recall_quarantine",
        "inventory_exception",
        "connector_duplicate_invalid",
        "multimodal_review",
        "auth_tenant_isolation",
        "worker_retry_dlq",
        "hitl_approval_rejection",
        "writeback",
        "learning",
    )


def test_integrity_audit_rejects_decision_reuse_mismatch_noop_and_zero_live_answers() -> None:
    trail = [
        {
            "decision_id": "dec_reused",
            "requested_action": "approve",
            "matched": False,
            "mismatches": ["returned_status=rejected expected=approved"],
            "learning_movement_expected": True,
            "learning_delta": 0,
        },
        {
            "decision_id": "dec_reused",
            "requested_action": "duplicate",
            "matched": False,
            "learning_movement_expected": False,
            "learning_delta": 0,
        },
    ]

    failures = audit_full_system_integrity(
        decision_trail=trail,
        feature_receipts=_passing_feature_receipts(),
        route_receipts=_passing_route_receipts(),
        live_required=True,
        chat_calls=1,
        chat_model_answers=0,
        chat_offline_answers=1,
    )

    assert any(item.startswith("decision_reuse:dec_reused") for item in failures)
    assert any(item.startswith("hitl_request_result_mismatch:dec_reused") for item in failures)
    assert any(item.startswith("learning_noop:dec_reused") for item in failures)
    assert "live_model_answer_mismatch:model=0:calls=1" in failures
    assert "live_offline_answers:1" in failures
    assert "missing_feature_receipt:agentic_workflows" in failures
    assert any(
        failure == f"missing_route_receipt:{route}"
        for route in LIVE_REQUIRED_ROUTE_RECEIPTS
        for failure in failures
    )


def test_integrity_audit_allows_one_deterministic_agentic_convergence() -> None:
    trail = [
        {
            "decision_id": "dec_shared",
            "scenario_id": "procurement_reorder_supplier_cover",
            "source": "demo:procurement",
            "requested_action": "approve",
            "matched": True,
        },
        {
            "decision_id": "dec_shared",
            "scenario_id": "procurement_reorder_supplier_cover",
            "source": "agentic_workflows:/scenarios/procurement/agentic",
            "requested_action": "duplicate",
            "matched": False,
        },
    ]

    failures = audit_full_system_integrity(
        decision_trail=trail,
        feature_receipts=_passing_feature_receipts(),
        route_receipts=_passing_route_receipts(),
        live_required=False,
        chat_calls=1,
        chat_model_answers=0,
    )

    assert not any(item.startswith("decision_reuse:dec_shared") for item in failures)


def test_integrity_audit_still_rejects_same_source_decision_reuse() -> None:
    trail = [
        {
            "decision_id": "dec_bad_reuse",
            "scenario_id": "procurement_reorder_supplier_cover",
            "source": "demo:procurement",
        },
        {
            "decision_id": "dec_bad_reuse",
            "scenario_id": "procurement_reorder_supplier_cover",
            "source": "demo:procurement",
        },
    ]

    failures = audit_full_system_integrity(
        decision_trail=trail,
        feature_receipts=_passing_feature_receipts(),
        route_receipts=_passing_route_receipts(),
        live_required=False,
        chat_calls=1,
        chat_model_answers=0,
    )

    assert "decision_reuse:dec_bad_reuse:count=2" in failures


def test_live_only_receipts_cover_all_agentic_workflows() -> None:
    assert {"agentic_workflows", "agent_role_coverage"} == LIVE_REQUIRED_FEATURE_RECEIPTS
    assert {
        "POST /scenarios/golden/agentic",
        "POST /scenarios/procurement/agentic",
        "POST /scenarios/sales/agentic",
        "POST /scenarios/catalog-price/agentic",
        "POST /scenarios/expiry-risk/agentic",
        "POST /scenarios/cold-chain/agentic",
    } == LIVE_REQUIRED_ROUTE_RECEIPTS


def test_integrity_audit_requires_feature_and_route_receipts() -> None:
    failures = audit_full_system_integrity(
        decision_trail=[],
        feature_receipts=[],
        route_receipts=[],
        live_required=False,
        chat_calls=0,
        chat_model_answers=0,
    )

    assert f"missing_feature_receipt:{next(iter(REQUIRED_FEATURE_RECEIPTS))}" in failures
    assert f"missing_route_receipt:{next(iter(REQUIRED_ROUTE_RECEIPTS))}" in failures


def test_real_full_system_rotation_passes_and_exports_receipts(tmp_path) -> None:
    artifact_dir = tmp_path / "stress_test"
    report = run_full_system(
        FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            assortment_sizes=(None,),
            chat_every_n_cycles=1,
            artifact_dir=artifact_dir,
            run_id="test_full_system",
        )
    )

    assert report.passed is True, report.failures
    assert report.exit_code == 0
    assert report.totals["approved"] > 0
    assert report.totals["rejected"] > 0
    assert report.totals["hitl_mismatches"] == 0
    assert report.totals["learning_movements"] > 0
    assert report.totals["world_events_submitted"] == report.totals["world_events_accepted"]
    assert set(report.event_contract["observed_types"]) == {
        "cold_chain_alert",
        "expiry_entry",
        "sale",
        "scan",
        "shipment",
        "stock_update",
        "supplier_update",
        "recall_notice",
        "inventory_exception",
    }

    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    trail = (artifact_dir / "decision_trail.jsonl").read_text(encoding="utf-8").splitlines()
    assert manifest["passed"] is True
    assert manifest["totals"]["unique_decision_ids"] == manifest["totals"]["decisions_total"]
    assert len(trail) == report.totals["decisions_total"]
    assert (artifact_dir / "feature_receipts.json").exists()
    assert (artifact_dir / "route_receipts.json").exists()


def test_default_base_seed_does_not_repeat_between_consecutive_runs(monkeypatch) -> None:
    """Before this fix every default (no `--base-seed`) run reused the exact same
    20_260_710 seed, so two consecutive soak runs deterministically replayed the same
    (seed, scenario) cycle pairs (the 2026-07-14 forensic audit's "the two runs share half
    their data" finding). Two configs built seconds apart must not share a single per-cycle
    seed value.
    """
    timestamps = iter([1_700_000_000_000_000_000, 1_700_000_003_000_000_000])
    monkeypatch.setattr(full_system.time, "time_ns", lambda: next(timestamps))

    first = FullSystemConfig(
        world_cycles=len(full_system.SCENARIOS), assortment_sizes=(None,)
    )
    second = FullSystemConfig(
        world_cycles=len(full_system.SCENARIOS), assortment_sizes=(None,)
    )

    assert first.base_seed != second.base_seed
    first_seeds = {first.base_seed + cycle for cycle in range(first.world_cycles)}
    second_seeds = {second.base_seed + cycle for cycle in range(second.world_cycles)}
    assert first_seeds.isdisjoint(second_seeds)


def test_explicit_base_seed_still_reproduces_the_same_world_exactly(tmp_path) -> None:
    """`--base-seed 20260710` (the historical always-on default) must still reproduce an
    identical world - reproduction stays one flag away even though the default now varies
    per run when the flag is omitted.
    """
    common_kwargs = dict(
        base_seed=20_260_710,
        world_cycles=3,
        event_limit=9,
        assortment_sizes=(None,),
        chat_every_n_cycles=1,
    )
    first = run_full_system(
        FullSystemConfig(
            **common_kwargs, artifact_dir=tmp_path / "repro_a", run_id="repro_a"
        )
    )
    second = run_full_system(
        FullSystemConfig(
            **common_kwargs, artifact_dir=tmp_path / "repro_b", run_id="repro_b"
        )
    )

    assert first.totals["world_events_generated"] == second.totals["world_events_generated"]
    assert first.totals["world_events_submitted"] == second.totals["world_events_submitted"]
    cycles_a = (tmp_path / "repro_a" / "cycles.jsonl").read_text(encoding="utf-8")
    cycles_b = (tmp_path / "repro_b" / "cycles.jsonl").read_text(encoding="utf-8")
    assert cycles_a == cycles_b


def test_manifest_carries_per_route_latency_percentiles(tmp_path) -> None:
    """The 2026-07-14 forensic audit found "what is p95 for /chat?" unanswerable from any
    past run's artifacts. Every route receipt must carry its own duration, and the manifest
    must summarize p50/p95/p99/max per route key so the question is answerable from every
    future run without a live re-run.
    """
    artifact_dir = tmp_path / "latency_receipts"
    report = run_full_system(
        FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            assortment_sizes=(None,),
            chat_every_n_cycles=1,
            artifact_dir=artifact_dir,
            run_id="test_route_latency",
        )
    )

    assert report.passed is True, report.failures
    assert report.route_receipts, "expected at least one route receipt"
    assert all(receipt.duration_ms >= 0 for receipt in report.route_receipts)

    latency = report.totals["route_latency"]
    assert latency, "expected a non-empty per-route latency summary"
    sample_key = next(iter(latency))
    assert set(latency[sample_key]) == {"count", "p50_ms", "p95_ms", "p99_ms", "max_ms"}
    assert latency[sample_key]["count"] > 0

    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["totals"]["route_latency"] == latency
    route_receipts = json.loads((artifact_dir / "route_receipts.json").read_text(encoding="utf-8"))
    assert all("duration_ms" in receipt for receipt in route_receipts)


def test_periodic_agentic_receipts_scale_with_world_cycles(monkeypatch) -> None:
    """B3 must execute inside the rotation, not merely define an unused helper.

    Six cycles at an interval of two produce three periodic executions. The end-of-run
    one-shot sweep remains a separate floor and is intentionally not part of this focused
    rotation test.
    """
    from shelfwise_backend.app import app

    runtime = full_system._load_runtime()
    full_system._reset_in_memory_state(runtime)
    config = FullSystemConfig(
        world_cycles=6,
        event_limit=9,
        chat_every_n_cycles=100,
        agentic_every_n_cycles=2,
        live_required=True,
    )
    driver = full_system._FullSystemDriver(
        config=config,
        runtime=runtime,
        client=TestClient(app),
    )
    executed_cycles: list[int] = []
    monkeypatch.setattr(driver, "_ask_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        driver,
        "_run_periodic_agentic_probe",
        lambda cycle: executed_cycles.append(cycle),
    )

    driver._drive_world_rotation()

    assert executed_cycles == [1, 3, 5]
    assert full_system._public_config(config)["agentic_every_n_cycles"] == 2


def test_agentic_execution_totals_are_exported_by_cascade() -> None:
    from shelfwise_backend.app import app

    runtime = full_system._load_runtime()
    full_system._reset_in_memory_state(runtime)
    driver = full_system._FullSystemDriver(
        config=FullSystemConfig(world_cycles=3, event_limit=9),
        runtime=runtime,
        client=TestClient(app),
    )
    driver.agentic_executions_by_cascade.update(
        {"/scenarios/golden/agentic": 3, "/scenarios/cold-chain/agentic": 2}
    )

    report = driver._build_report()

    assert report.totals["agentic_executions_by_cascade"] == {
        "/scenarios/cold-chain/agentic": 2,
        "/scenarios/golden/agentic": 3,
    }


def test_dissenting_autopilot_rejects_without_writeback_and_records_learning(tmp_path) -> None:
    artifact_dir = tmp_path / "dissenting_autopilot"
    report = run_full_system(
        FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            chat_every_n_cycles=1,
            autopilot_dissent_every_n=2,
            artifact_dir=artifact_dir,
            run_id="dissenting_autopilot",
        )
    )

    assert report.passed is True, report.failures
    eligible = report.totals["autopilot_approvable"]
    dissents = report.totals["autopilot_dissent_rejections"]
    assert eligible >= 2
    assert dissents == eligible // 2
    assert 0.25 <= report.totals["autopilot_dissent_rate"] <= 0.5
    assert report.totals["rejected_writeback_tasks"] == 0

    learning = json.loads((artifact_dir / "learning_events.json").read_text(encoding="utf-8"))
    rejected = [
        event
        for event in learning["events"]
        if event.get("outcome", {}).get("decision_status") == "rejected"
    ]
    assert len(rejected) >= dissents
    assert all(event["delta_units"] == 0 for event in rejected)


def test_chat_corpus_includes_off_catalog_multi_turn_and_hostile_cases() -> None:
    product = type("Product", (), {"name": "Milk", "department": "Dairy"})()
    cases = [
        full_system._chat_case(product, cycle=cycle, run_id="corpus")
        for cycle in range(5)
    ]

    assert [case.corpus for case in cases] == [
        "product_template",
        "off_catalog",
        "multi_turn",
        "multi_turn",
        "hostile",
    ]
    assert cases[2].conversation_id == cases[3].conversation_id
    assert cases[2].message_id != cases[3].message_id
    assert "\u200b" in cases[4].question and "\u202e" in cases[4].question


def test_offline_soak_exports_chat_corpus_breakdown_and_full_samples(tmp_path) -> None:
    artifact_dir = tmp_path / "chat_corpus"
    report = run_full_system(
        FullSystemConfig(
            world_cycles=5,
            event_limit=9,
            chat_every_n_cycles=1,
            artifact_dir=artifact_dir,
            run_id="chat_corpus",
        )
    )

    assert report.passed is True, report.failures
    assert report.totals["chat_corpus_breakdown"] == {
        "hostile": 1,
        "multi_turn": 2,
        "off_catalog": 1,
        "product_template": 1,
    }
    samples = json.loads((artifact_dir / "chat_samples.json").read_text(encoding="utf-8"))
    assert len(samples) == 5
    assert all("conversation_id" in sample and "message_id" in sample for sample in samples)
    assert next(sample for sample in samples if sample["corpus"] == "hostile")["question"] == (
        full_system._HOSTILE_CHAT_QUESTION
    )


def test_chat_sample_keeps_the_full_bounded_answer() -> None:
    from shelfwise_backend.app import app

    runtime = full_system._load_runtime()
    full_system._reset_in_memory_state(runtime)
    driver = full_system._FullSystemDriver(
        config=FullSystemConfig(world_cycles=3, event_limit=9),
        runtime=runtime,
        client=TestClient(app),
    )
    case = full_system._ChatCase(0, "hostile", "question", "conversation", "message")
    answer = "evidence " * 200

    driver._record_chat_sample(case, answer, 12.5, True, [])

    assert driver.chat_samples[0]["answer"] == answer


@pytest.mark.parametrize("kind", full_system._FAULT_KINDS)
def test_each_fault_type_is_rejected_without_state_mutation(kind, monkeypatch) -> None:
    from shelfwise_backend.app import app

    monkeypatch.setenv("SHELFWISE_MAX_BODY_BYTES", "512")
    runtime = full_system._load_runtime()
    full_system._reset_in_memory_state(runtime)
    scenario_id = next(iter(full_system.SCENARIOS))
    world, _ = full_system.build(scenario_id, seed_override=101, tenant_id="local")
    event = next(iter(world.run()))
    driver = full_system._FullSystemDriver(
        config=FullSystemConfig(world_cycles=3, event_limit=9),
        runtime=runtime,
        client=TestClient(app),
    )
    if kind == "duplicate_event_id":
        seeded = driver._request(
            "fault_setup",
            "POST",
            "/ingest",
            json=event.to_dict(),
        )
        assert seeded.status_code == 200
        driver._last_accepted_event = event.to_dict()

    rejected = driver._inject_fault(event, kind)

    assert rejected is True
    assert driver.totals["faults_injected"] == 1
    assert driver.totals["faults_correctly_rejected"] == 1


def test_memory_backend_run_passes_because_faults_are_safely_rejected(tmp_path) -> None:
    report = run_full_system(
        FullSystemConfig(
            world_cycles=5,
            event_limit=9,
            chat_every_n_cycles=1,
            fault_rate=0.1,
            artifact_dir=tmp_path / "fault_campaign",
            run_id="fault_campaign",
        )
    )

    assert report.passed is True, report.failures
    assert report.totals["faults_injected"] > 0
    assert report.totals["faults_correctly_rejected"] == report.totals["faults_injected"]
    assert report.totals["world_events_accepted"] == (
        report.totals["world_events_submitted"] - report.totals["faults_injected"]
    )


def test_blackout_fails_closed_and_recovers_within_one_cycle(monkeypatch) -> None:
    from shelfwise_backend.app import app

    runtime = full_system._load_runtime()
    full_system._reset_in_memory_state(runtime)
    driver = full_system._FullSystemDriver(
        config=FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            live_required=True,
            blackout_seconds=0.01,
        ),
        runtime=runtime,
        client=TestClient(app),
    )
    requested: list[str] = []

    def fail_closed(_feature, _method, path, **_kwargs):
        requested.append(path)
        return type("Response", (), {"status_code": 503, "text": ""})()

    monkeypatch.setattr(driver, "_request", fail_closed)
    monkeypatch.setattr(full_system.time, "sleep", lambda _seconds: None)

    driver._probe_inference_blackout(cycle=1)
    driver._observe_blackout_recovery(
        full_system._ChatCase(2, "product_template", "question", "conversation", "message"),
        type("Response", (), {"status_code": 200})(),
        True,
    )

    assert len(requested) == 7
    assert driver.totals["blackout_routes_failed_closed"] == 7
    assert driver.totals["blackout_recovery_cycles"] == 1
    assert next(item for item in driver.features if item.feature == "inference_blackout").passed


def test_a_crash_mid_run_still_writes_a_best_effort_manifest(tmp_path, monkeypatch) -> None:
    """A real GPU soak run can be interrupted at any point - SSH drop, droplet timeout, an
    unhandled exception. Before this fix, manifest.json/feature_receipts.json/etc were only
    written after every probe phase completed, so an interruption left nothing but the raw,
    unsummarized decision_trail.jsonl/cycles.jsonl - no readable report of what happened.
    """
    artifact_dir = tmp_path / "interrupted_run"

    def _boom(self) -> None:
        raise RuntimeError("simulated droplet/network interruption")

    monkeypatch.setattr(full_system._FullSystemDriver, "_probe_misprice", _boom)

    with pytest.raises(RuntimeError, match="simulated droplet/network interruption"):
        run_full_system(
            FullSystemConfig(
                world_cycles=3,
                event_limit=9,
                assortment_sizes=(None,),
                chat_every_n_cycles=1,
                artifact_dir=artifact_dir,
                run_id="test_full_system_interrupted",
            )
        )

    manifest_path = artifact_dir / "manifest.json"
    assert manifest_path.exists(), "manifest.json must exist even when the run is interrupted"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["passed"] is False
    assert any(
        failure.startswith("run_interrupted:RuntimeError:simulated droplet/network interruption")
        for failure in manifest["failures"]
    )
    # Phases that ran before the interruption (world rotation) must still be reflected -
    # this is not an empty stub, it is everything genuinely accumulated so far.
    assert manifest["totals"]["world_cycles"] > 0
    assert (artifact_dir / "feature_receipts.json").exists()
    assert (artifact_dir / "route_receipts.json").exists()


def test_reusing_an_output_dir_with_a_prior_manifest_is_refused_by_default(tmp_path) -> None:
    """A copy-pasted command or a retry-after-crash reusing the same --output-dir must not
    silently destroy the previous (expensive, hard-to-reproduce) GPU run's data."""
    artifact_dir = tmp_path / "reused_run"
    config = FullSystemConfig(
        world_cycles=3,
        event_limit=9,
        assortment_sizes=(None,),
        chat_every_n_cycles=1,
        artifact_dir=artifact_dir,
        run_id="first_run",
    )
    first = run_full_system(config)
    assert first.exit_code == 0

    with pytest.raises(FileExistsError, match="already exists from a previous run"):
        run_full_system(
            FullSystemConfig(
                world_cycles=3,
                event_limit=9,
                assortment_sizes=(None,),
                chat_every_n_cycles=1,
                artifact_dir=artifact_dir,
                run_id="second_run",
            )
        )

    # The first run's manifest must be untouched by the refused second attempt.
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "first_run"

    # Explicit opt-in still allows overwriting.
    second = run_full_system(
        FullSystemConfig(
            world_cycles=3,
            event_limit=9,
            assortment_sizes=(None,),
            chat_every_n_cycles=1,
            artifact_dir=artifact_dir,
            run_id="second_run",
            allow_overwrite_artifact_dir=True,
        )
    )
    assert second.exit_code == 0
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "second_run"


def test_reset_clears_every_store_the_harness_and_app_touch(tmp_path, monkeypatch) -> None:
    """Synthetic test data must be trivially removable after a soak run - `reset_state=True`
    (the default) previously left candidate/twin/edge/connector-cursor/chat/open-order/
    inventory/world-snapshot state behind because `_reset_in_memory_state` predates those
    stores. Populate every one of them directly, run the reset, and assert they are empty."""
    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SHELFWISE_BUS_BACKEND", "memory")
    runtime = full_system._load_runtime()

    from datetime import UTC, datetime

    from shelfwise_backend.candidate_factory import generate_fleet_candidates
    from shelfwise_edge import EdgeDevice
    from shelfwise_twin.models import TwinObservation

    candidate = generate_fleet_candidates(
        [
            {
                "sku": "SKU-RESET-1",
                "name": "Reset Test",
                "category": "Dairy",
                "supplier": "Sup",
                "on_hand": 4,
                "reorder_point": 20,
                "days_to_expiry": 3,
                "attention_reasons": ["low_stock"],
                "batches": [],
            }
        ],
        tenant_id="sa_retail_demo",
    )[0]
    runtime.candidate_store.upsert(candidate)
    assert runtime.candidate_store.list("sa_retail_demo") != []

    runtime.twin_service.accept(
        TwinObservation(
            observation_id="obs_reset_test",
            tenant_id="sa_retail_demo",
            store_id="store_reset_test",
            twin_id="urn:shelfwise:sa_retail_demo:store_reset_test:fixture:fridge_1",
            property_name="cold_chain.status",
            lane="reported",
            value="healthy",
            observed_at=datetime.now(UTC),
            source_system="api",
            source_object_id="reset-test-1",
            source_quality=1.0,
            correlation_id="cor-reset-test",
            payload_hash="c" * 64,
        )
    )
    assert runtime.twin_service.store.list_observations("sa_retail_demo") != []

    runtime.edge_device_registry.register(
        EdgeDevice(
            device_id="device_reset_test",
            tenant_id="sa_retail_demo",
            store_id="store_reset_test",
            hmac_secret=b"reset-test-secret",
        )
    )
    assert runtime.edge_device_registry.get_active("device_reset_test") is not None

    import asyncio

    from shelfwise_connectors.canonical import SourceSystem

    asyncio.run(
        runtime.connector_cursor_store.set(
            tenant_id="sa_retail_demo", system=SourceSystem.SAP, cursor="cursor-reset-test"
        )
    )
    assert (
        asyncio.run(
            runtime.connector_cursor_store.get(
                tenant_id="sa_retail_demo", system=SourceSystem.SAP
            )
        )
        is not None
    )

    full_system._reset_in_memory_state(runtime)

    assert runtime.candidate_store.list("sa_retail_demo") == []
    assert runtime.twin_service.store.list_observations("sa_retail_demo") == []
    assert runtime.edge_device_registry.get_active("device_reset_test") is None
    assert (
        asyncio.run(
            runtime.connector_cursor_store.get(
                tenant_id="sa_retail_demo", system=SourceSystem.SAP
            )
        )
        is None
    )
