from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .models import (
    STATUS_RANK,
    AgentCapability,
    CapabilityManifest,
    CapabilityPolicy,
    CapabilityStatus,
    ConnectorCapability,
    EventConsumerCapability,
    EventTypeCapability,
    RelationshipKind,
    TrainingStageCapability,
    WaiverRule,
)
from .serialization import build_manifest


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """One machine-readable capability-contract failure."""

    code: str
    message: str
    capability_id: str | None = None

    def render(self) -> str:
        """Render a concise failure line for CLI and pytest output."""
        target = f" [{self.capability_id}]" if self.capability_id else ""
        return f"{self.code}{target}: {self.message}"


class CapabilityContractError(AssertionError):
    """Raised when one or more capability-contract checks fail."""

    def __init__(self, violations: list[ContractViolation]) -> None:
        self.violations = violations
        detail = "\n".join(f"- {item.render()}" for item in violations)
        super().__init__(f"capability contract failed:\n{detail}")


def assert_contract(
    manifest: CapabilityManifest,
    policy: CapabilityPolicy,
    repo_root: Path,
    *,
    discovered: CapabilityManifest | None = None,
    today: date | None = None,
) -> None:
    """Raise when the current manifest, source links, or discovery snapshot is invalid."""
    violations = collect_contract_violations(
        manifest,
        policy,
        repo_root,
        discovered=discovered,
        today=today,
    )
    if violations:
        raise CapabilityContractError(violations)


def collect_contract_violations(
    manifest: CapabilityManifest,
    policy: CapabilityPolicy,
    repo_root: Path,
    *,
    discovered: CapabilityManifest | None = None,
    today: date | None = None,
) -> list[ContractViolation]:
    """Collect structural, evidence, waiver, and optional source-drift failures."""
    check_date = today or datetime.now(UTC).date()
    violations: list[ContractViolation] = []
    capabilities = manifest.capabilities
    by_id = {item.id: item for item in capabilities}

    if len(by_id) != len(capabilities):
        violations.append(
            ContractViolation("duplicate_capability_id", "capability ids must be unique")
        )

    normalized = build_manifest(list(capabilities))
    if manifest.fingerprint != normalized.fingerprint:
        violations.append(
            ContractViolation(
                "fingerprint_mismatch",
                f"expected {normalized.fingerprint}, found {manifest.fingerprint}",
            )
        )

    discovered_kinds = {item.kind for item in capabilities}
    for kind in policy.required_kinds:
        if kind not in discovered_kinds:
            violations.append(
                ContractViolation("missing_capability_kind", f"required kind {kind.value} is empty")
            )
    for capability_id in policy.required_capability_ids:
        if capability_id not in by_id and not _is_waived(
            policy, capability_id, WaiverRule.OMISSION, check_date
        ):
            violations.append(
                ContractViolation(
                    "missing_required_capability",
                    "required capability is absent",
                    capability_id,
                )
            )
    for capability_id in policy.annotations:
        if capability_id not in by_id:
            violations.append(
                ContractViolation(
                    "orphan_policy_annotation",
                    "policy annotation does not resolve to a discovered capability",
                    capability_id,
                )
            )

    for capability in capabilities:
        violations.extend(_validate_sources(capability.id, capability.sources, repo_root))
        for relationship in capability.relationships:
            if relationship.target == capability.id:
                violations.append(
                    ContractViolation(
                        "self_relationship",
                        f"{relationship.kind.value} relationship targets itself",
                        capability.id,
                    )
                )
            elif relationship.target not in by_id:
                violations.append(
                    ContractViolation(
                        "unresolved_relationship",
                        f"{relationship.kind.value} target {relationship.target} is absent",
                        capability.id,
                    )
                )

        if capability.status in policy.verification_required_statuses:
            if not capability.verification_nodeids and not _is_waived(
                policy,
                capability.id,
                WaiverRule.MISSING_VERIFICATION,
                check_date,
            ):
                violations.append(
                    ContractViolation(
                        "missing_verification_nodeids",
                        f"status {capability.status.value} requires at least one pytest nodeid",
                        capability.id,
                    )
                )
            for nodeid in capability.verification_nodeids:
                if not _nodeid_resolves(repo_root, nodeid):
                    violations.append(
                        ContractViolation(
                            "unresolved_verification_nodeid",
                            f"pytest nodeid does not resolve statically: {nodeid}",
                            capability.id,
                        )
                    )

        violations.extend(_validate_typed_claims(capability, by_id))

    violations.extend(_validate_waivers(policy, check_date))
    if discovered is not None:
        violations.extend(compare_discovered_manifest(manifest, discovered, policy, check_date))
    return violations


def compare_discovered_manifest(
    committed: CapabilityManifest,
    discovered: CapabilityManifest,
    policy: CapabilityPolicy,
    today: date,
) -> list[ContractViolation]:
    """Report committed snapshot omissions, stale rows, and source-derived drift."""
    violations: list[ContractViolation] = []
    committed_by_id = {item.id: item for item in committed.capabilities}
    discovered_by_id = {item.id: item for item in discovered.capabilities}
    for capability_id in sorted(discovered_by_id.keys() - committed_by_id.keys()):
        if _is_waived(policy, capability_id, WaiverRule.DISCOVERY_MISMATCH, today):
            continue
        violations.append(
            ContractViolation(
                "discovery_omission",
                "source discovery found a capability missing from the committed manifest",
                capability_id,
            )
        )
    for capability_id in sorted(committed_by_id.keys() - discovered_by_id.keys()):
        if _is_waived(policy, capability_id, WaiverRule.DISCOVERY_MISMATCH, today):
            continue
        violations.append(
            ContractViolation(
                "stale_capability",
                "committed capability is no longer discovered from source",
                capability_id,
            )
        )
    for capability_id in sorted(committed_by_id.keys() & discovered_by_id.keys()):
        committed_payload = committed_by_id[capability_id].model_dump(mode="json")
        discovered_payload = discovered_by_id[capability_id].model_dump(mode="json")
        if committed_payload == discovered_payload:
            continue
        if _is_waived(policy, capability_id, WaiverRule.DISCOVERY_MISMATCH, today):
            continue
        violations.append(
            ContractViolation(
                "discovery_mismatch",
                "committed capability fields differ from deterministic discovery",
                capability_id,
            )
        )
    return violations


def compare_baseline_manifests(
    baseline: CapabilityManifest,
    current: CapabilityManifest,
    policy: CapabilityPolicy,
    *,
    today: date | None = None,
) -> list[ContractViolation]:
    """Detect capability regressions relative to a base-branch manifest."""
    check_date = today or datetime.now(UTC).date()
    violations: list[ContractViolation] = []
    baseline_by_id = {item.id: item for item in baseline.capabilities}
    current_by_id = {item.id: item for item in current.capabilities}
    for capability_id, previous in sorted(baseline_by_id.items()):
        current_item = current_by_id.get(capability_id)
        if current_item is None:
            if not _is_waived(policy, capability_id, WaiverRule.OMISSION, check_date):
                violations.append(
                    ContractViolation(
                        "capability_omission",
                        "baseline capability was removed",
                        capability_id,
                    )
                )
            continue
        if STATUS_RANK[current_item.status] < STATUS_RANK[previous.status] and not _is_waived(
            policy, capability_id, WaiverRule.DOWNGRADE, check_date
        ):
            violations.append(
                ContractViolation(
                    "capability_downgrade",
                    f"status fell from {previous.status.value} to {current_item.status.value}",
                    capability_id,
                )
            )
        previous_edges = {(edge.kind, edge.target) for edge in previous.relationships}
        current_edges = {(edge.kind, edge.target) for edge in current_item.relationships}
        removed_edges = previous_edges - current_edges
        if removed_edges and not _is_waived(
            policy,
            capability_id,
            WaiverRule.RELATIONSHIP_REMOVAL,
            check_date,
        ):
            rendered = ", ".join(
                f"{kind.value}:{target}" for kind, target in sorted(removed_edges)
            )
            violations.append(
                ContractViolation(
                    "relationship_removal",
                    f"baseline relationships removed: {rendered}",
                    capability_id,
                )
            )
        removed_nodeids = set(previous.verification_nodeids) - set(
            current_item.verification_nodeids
        )
        if removed_nodeids and not _is_waived(
            policy,
            capability_id,
            WaiverRule.MISSING_VERIFICATION,
            check_date,
        ):
            violations.append(
                ContractViolation(
                    "verification_removal",
                    f"baseline nodeids removed: {', '.join(sorted(removed_nodeids))}",
                    capability_id,
                )
            )
    return violations


def _validate_sources(
    capability_id: str,
    sources: list[object],
    repo_root: Path,
) -> list[ContractViolation]:
    """Check every source pointer resolves to a real file and line."""
    violations: list[ContractViolation] = []
    for source in sources:
        path = repo_root / source.path  # type: ignore[attr-defined]
        if not path.is_file():
            violations.append(
                ContractViolation(
                    "missing_source_file",
                    f"source path does not exist: {source.path}",  # type: ignore[attr-defined]
                    capability_id,
                )
            )
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines()) or 1
        if source.line > line_count:  # type: ignore[attr-defined]
            violations.append(
                ContractViolation(
                    "invalid_source_line",
                    f"source line {source.line} exceeds {line_count}: {source.path}",  # type: ignore[attr-defined]
                    capability_id,
                )
            )
    return violations


def _validate_typed_claims(capability: object, by_id: dict[str, object]) -> list[ContractViolation]:
    """Enforce category-specific honesty and relationship invariants."""
    violations: list[ContractViolation] = []
    capability_id = capability.id  # type: ignore[attr-defined]
    if isinstance(capability, ConnectorCapability):
        if capability.status is CapabilityStatus.DECLARATION_ONLY and (
            capability.catalogued or capability.mapped
        ):
            violations.append(
                ContractViolation(
                    "connector_status_conflict",
                    "catalogued or mapped connector cannot be declaration-only",
                    capability.id,
                )
            )
        if (
            not capability.catalogued
            and not capability.mapped
            and capability.status is not CapabilityStatus.DECLARATION_ONLY
        ):
            violations.append(
                ContractViolation(
                    "unmapped_connector_overstated",
                    "uncatalogued and unmapped connector must remain declaration-only",
                    capability.id,
                )
            )
        if capability.mapper_registered_claim and not capability.mapped:
            violations.append(
                ContractViolation(
                    "connector_mapper_claim_conflict",
                    "catalogue claims a mapper but deterministic discovery found none",
                    capability.id,
                )
            )
    if (
        isinstance(capability, AgentCapability)
        and capability.agent_name == "orchestrator"
        and capability.metadata_only
        and capability.status is not CapabilityStatus.DECLARATION_ONLY
    ):
        violations.append(
            ContractViolation(
                "orchestrator_overstated",
                "metadata-only orchestrator must remain declaration-only",
                capability.id,
            )
        )
    if (
        isinstance(capability, AgentCapability)
        and capability.agent_name == "orchestrator"
        and not capability.metadata_only
        and capability.status is CapabilityStatus.DECLARATION_ONLY
    ):
        violations.append(
            ContractViolation(
                "orchestrator_understated",
                "discovered AgentOrchestrator implementation cannot be declaration-only",
                capability.id,
            )
        )
    if isinstance(capability, EventTypeCapability):
        for consumer_id in capability.consumers:
            consumer = by_id.get(consumer_id)
            if not isinstance(consumer, EventConsumerCapability):
                violations.append(
                    ContractViolation(
                        "unresolved_event_consumer",
                        f"consumer list target is absent or wrong type: {consumer_id}",
                        capability.id,
                    )
                )
    if isinstance(capability, EventConsumerCapability):
        consumes = {
            edge.target
            for edge in capability.relationships
            if edge.kind is RelationshipKind.CONSUMES
        }
        expected = f"event_type:{capability.event_type}"
        if expected not in consumes:
            violations.append(
                ContractViolation(
                    "event_consumer_relationship_missing",
                    f"consumer must link to {expected}",
                    capability.id,
                )
            )
    if (
        isinstance(capability, TrainingStageCapability)
        and capability.runtime_verified
        and STATUS_RANK[capability.status]
        < STATUS_RANK[
            CapabilityStatus.VERIFIED
        ]
    ):
        violations.append(
            ContractViolation(
                "training_runtime_status_conflict",
                "runtime_verified requires verified or demo_verified status",
                capability.id,
            )
        )
    _ = capability_id
    return violations


def _validate_waivers(
    policy: CapabilityPolicy,
    today: date,
) -> list[ContractViolation]:
    """Reject duplicate, expired, or excessively long waiver records."""
    violations: list[ContractViolation] = []
    ids = [waiver.id for waiver in policy.waivers]
    if len(ids) != len(set(ids)):
        violations.append(ContractViolation("duplicate_waiver_id", "waiver ids must be unique"))
    maximum = today + timedelta(days=policy.max_waiver_days)
    for waiver in policy.waivers:
        if waiver.expires_on < today:
            violations.append(
                ContractViolation(
                    "expired_waiver",
                    f"waiver expired on {waiver.expires_on.isoformat()}",
                    waiver.capability_id,
                )
            )
        elif waiver.expires_on > maximum:
            violations.append(
                ContractViolation(
                    "waiver_window_too_long",
                    (
                        f"waiver expires after the {policy.max_waiver_days}-day maximum: "
                        f"{waiver.expires_on.isoformat()}"
                    ),
                    waiver.capability_id,
                )
            )
    return violations


def _is_waived(
    policy: CapabilityPolicy,
    capability_id: str,
    rule: WaiverRule,
    today: date,
) -> bool:
    """Return whether an exact, active waiver suppresses one rule."""
    return any(
        waiver.capability_id == capability_id
        and rule in waiver.rules
        and today <= waiver.expires_on <= today + timedelta(days=policy.max_waiver_days)
        for waiver in policy.waivers
    )


def _nodeid_resolves(repo_root: Path, nodeid: str) -> bool:
    """Resolve a non-parameterized pytest file/function or class/function nodeid statically."""
    parts = nodeid.split("::")
    if len(parts) < 2 or "[" in nodeid:
        return False
    path = repo_root / parts[0]
    if not path.is_file() or path.suffix != ".py":
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return False
    if len(parts) == 2:
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[1]
            for node in tree.body
        )
    if len(parts) == 3:
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != parts[1]:
                continue
            return any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == parts[2]
                for child in node.body
            )
    return False
