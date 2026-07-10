from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import (
    Capability,
    CapabilityManifest,
    CapabilityPolicy,
    DeploymentProfileSnapshot,
    EventTypeCapability,
    StorageBackendCapability,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def canonical_json(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Serialize a model or JSON value with stable ordering and a final newline."""
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def normalize_capability(capability: Capability) -> Capability:
    """Return one capability with every set-like list deterministically ordered."""
    relationships = {
        f"{item.kind.value}\0{item.target}": item for item in capability.relationships
    }
    updates: dict[str, Any] = {
        "sources": sorted(
            capability.sources,
            key=lambda item: (item.path, item.line, item.symbol or ""),
        ),
        "verification_nodeids": sorted(set(capability.verification_nodeids)),
        "relationships": sorted(
            relationships.values(),
            key=lambda item: (item.kind.value, item.target),
        ),
    }
    if isinstance(capability, EventTypeCapability):
        updates["consumers"] = sorted(set(capability.consumers))
    if isinstance(capability, StorageBackendCapability):
        updates["factories"] = sorted(set(capability.factories))
    return capability.model_copy(update=updates)


def build_manifest(capabilities: list[Capability]) -> CapabilityManifest:
    """Normalize capabilities and calculate the committed content fingerprint."""
    normalized = sorted(
        (normalize_capability(item) for item in capabilities),
        key=lambda item: (item.kind.value, item.id),
    )
    fingerprint_payload = {
        "schema_version": "1.0",
        "generator": "shelfwise_capabilities",
        "capabilities": [item.model_dump(mode="json") for item in normalized],
    }
    digest = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return CapabilityManifest(
        fingerprint=f"sha256:{digest}",
        capabilities=normalized,
    )


def normalize_policy(policy: CapabilityPolicy) -> CapabilityPolicy:
    """Normalize policy lists while preserving their typed meaning."""
    annotations = {
        key: policy.annotations[key].model_copy(
            update={
                "verification_nodeids": (
                    sorted(set(policy.annotations[key].verification_nodeids or []))
                    if policy.annotations[key].verification_nodeids is not None
                    else None
                )
            }
        )
        for key in sorted(policy.annotations)
    }
    defaults = {
        key: sorted(set(policy.default_verification_nodeids[key]))
        for key in sorted(policy.default_verification_nodeids, key=lambda item: item.value)
    }
    waivers = sorted(
        (
            waiver.model_copy(
                update={"rules": sorted(set(waiver.rules), key=lambda item: item.value)}
            )
            for waiver in policy.waivers
        ),
        key=lambda item: item.id,
    )
    return policy.model_copy(
        update={
            "required_kinds": sorted(set(policy.required_kinds), key=lambda item: item.value),
            "required_capability_ids": sorted(set(policy.required_capability_ids)),
            "verification_required_statuses": sorted(
                set(policy.verification_required_statuses), key=lambda item: item.value
            ),
            "default_verification_nodeids": defaults,
            "annotations": annotations,
            "waivers": waivers,
        }
    )


def normalize_profiles(profiles: DeploymentProfileSnapshot) -> DeploymentProfileSnapshot:
    """Normalize profile ordering and source-path lists."""
    normalized = [
        profile.model_copy(update={"source_paths": sorted(set(profile.source_paths))})
        for profile in profiles.profiles
    ]
    return profiles.model_copy(update={"profiles": sorted(normalized, key=lambda item: item.id)})


def load_json_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """Load one strict contract model from a UTF-8 JSON file."""
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def manifest_schema() -> dict[str, Any]:
    """Return the JSON Schema generated from the typed manifest model."""
    return CapabilityManifest.model_json_schema()
