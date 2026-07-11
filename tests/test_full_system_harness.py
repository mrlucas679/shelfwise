from __future__ import annotations

import json

from shelfwise_eval.full_system import (
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
            event_limit=8,
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
    }

    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    trail = (artifact_dir / "decision_trail.jsonl").read_text(encoding="utf-8").splitlines()
    assert manifest["passed"] is True
    assert manifest["totals"]["unique_decision_ids"] == manifest["totals"]["decisions_total"]
    assert len(trail) == report.totals["decisions_total"]
    assert (artifact_dir / "feature_receipts.json").exists()
    assert (artifact_dir / "route_receipts.json").exists()
