from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from shelfwise_capabilities import (
    CapabilityManifest,
    CapabilityPolicy,
    CapabilityStatus,
    CapabilityWaiver,
    DeploymentProfileSnapshot,
    WaiverRule,
    assert_contract,
    build_manifest,
    canonical_json,
    collect_contract_violations,
    compare_baseline_manifests,
    discover_manifest,
    load_json_model,
    manifest_schema,
    normalize_policy,
    normalize_profiles,
)
from shelfwise_capabilities.models import CapabilityRelationship, RelationshipKind

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = ROOT / "capabilities"
CHECK_DATE = date(2026, 7, 10)


def _documents() -> tuple[CapabilityManifest, CapabilityPolicy, DeploymentProfileSnapshot]:
    """Load the committed typed contract documents."""
    manifest = load_json_model(CAPABILITIES / "manifest.json", CapabilityManifest)
    policy = normalize_policy(load_json_model(CAPABILITIES / "policy.json", CapabilityPolicy))
    profiles = normalize_profiles(
        load_json_model(CAPABILITIES / "profiles.json", DeploymentProfileSnapshot)
    )
    return manifest, policy, profiles


def _replace(
    manifest: CapabilityManifest,
    capability_id: str,
    **updates: object,
) -> CapabilityManifest:
    """Return a refingerprinted manifest with one capability mutated."""
    capabilities = [
        item.model_copy(update=updates) if item.id == capability_id else item
        for item in manifest.capabilities
    ]
    return build_manifest(capabilities)


def _codes(violations: list[object]) -> set[str]:
    """Return violation codes from a validation result."""
    return {item.code for item in violations}  # type: ignore[attr-defined]


def test_committed_capability_snapshot_matches_deterministic_discovery() -> None:
    """The committed manifest must exactly match a fresh static source scan."""
    manifest, policy, profiles = _documents()
    discovered = discover_manifest(ROOT, policy, profiles)

    assert discovered == manifest
    assert_contract(manifest, policy, ROOT, discovered=discovered, today=CHECK_DATE)


def test_manifest_schema_and_json_documents_are_normalized() -> None:
    """Schema, manifest, policy, and profiles remain reproducible byte-for-byte."""
    manifest, policy, profiles = _documents()

    assert json.loads((CAPABILITIES / "manifest.schema.json").read_text(encoding="utf-8")) == (
        manifest_schema()
    )
    assert (CAPABILITIES / "manifest.json").read_text(encoding="utf-8") == canonical_json(
        manifest
    )
    assert (CAPABILITIES / "policy.json").read_text(encoding="utf-8") == canonical_json(
        policy
    )
    assert (CAPABILITIES / "profiles.json").read_text(encoding="utf-8") == canonical_json(
        profiles
    )


def test_source_omission_fails_even_when_the_manifest_is_refingerprinted() -> None:
    """Removing a discovered row cannot be hidden by recalculating its fingerprint."""
    manifest, policy, profiles = _documents()
    discovered = discover_manifest(ROOT, policy, profiles)
    omitted = build_manifest(
        [item for item in manifest.capabilities if item.id != "agent:inventory"]
    )

    violations = collect_contract_violations(
        omitted,
        policy,
        ROOT,
        discovered=discovered,
        today=CHECK_DATE,
    )

    assert "discovery_omission" in _codes(violations)
    assert "unresolved_relationship" in _codes(violations)


def test_baseline_omissions_and_downgrades_fail() -> None:
    """Base-branch status and row regressions require an explicit waiver."""
    baseline, policy, _ = _documents()
    downgraded = _replace(
        baseline,
        "connector:sap",
        status=CapabilityStatus.PARTIAL,
    )
    omitted = build_manifest(
        [item for item in baseline.capabilities if item.id != "tool:platform:get_stock"]
    )

    assert "capability_downgrade" in _codes(
        compare_baseline_manifests(baseline, downgraded, policy, today=CHECK_DATE)
    )
    assert "capability_omission" in _codes(
        compare_baseline_manifests(baseline, omitted, policy, today=CHECK_DATE)
    )


def test_unresolved_relationship_and_removed_edge_fail() -> None:
    """Both dangling current edges and deleted baseline edges are regressions."""
    baseline, policy, _ = _documents()
    surface = next(
        item for item in baseline.capabilities if item.id == "frontend_surface:products"
    )
    dangling = _replace(
        baseline,
        surface.id,
        relationships=[
            *surface.relationships,
            CapabilityRelationship(
                kind=RelationshipKind.USES,
                target="openapi_route:get:/missing",
            ),
        ],
    )
    removed = _replace(baseline, surface.id, relationships=[])

    assert "unresolved_relationship" in _codes(
        collect_contract_violations(dangling, policy, ROOT, today=CHECK_DATE)
    )
    assert "relationship_removal" in _codes(
        compare_baseline_manifests(baseline, removed, policy, today=CHECK_DATE)
    )


def test_missing_or_unresolvable_verification_nodeids_fail() -> None:
    """Verified claims require nodeids that resolve to actual test functions."""
    manifest, policy, _ = _documents()
    missing = _replace(manifest, "bus_backend:memory", verification_nodeids=[])
    unresolved = _replace(
        manifest,
        "bus_backend:memory",
        verification_nodeids=["tests/test_store_backends.py::test_not_present"],
    )

    assert "missing_verification_nodeids" in _codes(
        collect_contract_violations(missing, policy, ROOT, today=CHECK_DATE)
    )
    assert "unresolved_verification_nodeid" in _codes(
        collect_contract_violations(unresolved, policy, ROOT, today=CHECK_DATE)
    )


def test_active_expiring_waiver_allows_only_its_named_regressions() -> None:
    """An exact active waiver suppresses named downgrade/evidence rules only."""
    baseline, policy, _ = _documents()
    current = _replace(
        baseline,
        "connector:sap",
        status=CapabilityStatus.PARTIAL,
        verification_nodeids=[],
    )
    waiver = CapabilityWaiver(
        id="waiver:sap-maintenance",
        capability_id="connector:sap",
        rules=[WaiverRule.DOWNGRADE, WaiverRule.MISSING_VERIFICATION],
        reason="Mapper maintenance temporarily removes verified SAP coverage.",
        owner="platform",
        expires_on=date(2026, 7, 20),
        issue="ACTII-142",
    )
    waived_policy = policy.model_copy(update={"waivers": [waiver]})

    assert compare_baseline_manifests(
        baseline,
        current,
        waived_policy,
        today=CHECK_DATE,
    ) == []
    assert "missing_verification_nodeids" not in _codes(
        collect_contract_violations(current, waived_policy, ROOT, today=CHECK_DATE)
    )


def test_expired_or_overlong_waivers_fail_closed() -> None:
    """Expired and beyond-policy waiver windows never suppress a regression."""
    baseline, policy, _ = _documents()
    current = _replace(
        baseline,
        "connector:sap",
        status=CapabilityStatus.PARTIAL,
    )
    expired = CapabilityWaiver(
        id="waiver:expired-sap",
        capability_id="connector:sap",
        rules=[WaiverRule.DOWNGRADE],
        reason="Historical SAP maintenance window is already closed.",
        owner="platform",
        expires_on=date(2026, 7, 9),
    )
    overlong = CapabilityWaiver(
        id="waiver:overlong-sap",
        capability_id="connector:sap",
        rules=[WaiverRule.DOWNGRADE],
        reason="Invalid SAP waiver exceeds the bounded exception window.",
        owner="platform",
        expires_on=date(2026, 9, 30),
    )
    expired_policy = policy.model_copy(update={"waivers": [expired]})
    overlong_policy = policy.model_copy(update={"waivers": [overlong]})

    assert "capability_downgrade" in _codes(
        compare_baseline_manifests(baseline, current, expired_policy, today=CHECK_DATE)
    )
    assert "expired_waiver" in _codes(
        collect_contract_violations(baseline, expired_policy, ROOT, today=CHECK_DATE)
    )
    assert "waiver_window_too_long" in _codes(
        collect_contract_violations(baseline, overlong_policy, ROOT, today=CHECK_DATE)
    )


def test_runtime_gated_claims_remain_honest() -> None:
    """Runtime-gated capabilities cannot silently look complete."""
    manifest, _, _ = _documents()
    by_id = {item.id: item for item in manifest.capabilities}

    orchestrator = by_id["agent:orchestrator"]
    assert orchestrator.status is CapabilityStatus.VERIFIED
    assert orchestrator.metadata_only is False  # type: ignore[union-attr]

    for event_type in ("inventory_exception", "recall_notice", "shipment", "stock_update"):
        event = by_id[f"event_type:{event_type}"]
        assert event.status is CapabilityStatus.VERIFIED
        assert event.consumers  # type: ignore[union-attr]

    for connector_id in ("connector:dynamics", "connector:yoco"):
        connector = by_id[connector_id]
        assert connector.status is CapabilityStatus.VERIFIED
        assert connector.catalogued is True  # type: ignore[union-attr]
        assert connector.mapped is True  # type: ignore[union-attr]

    assert by_id["training_stage:train"].status is CapabilityStatus.PARTIAL
    assert by_id["deployment_profile:mi300x_vllm_demo"].status is CapabilityStatus.PARTIAL
