from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StateLane(StrEnum):
    """Separate observed facts from estimates, intentions, and scenario predictions."""

    REPORTED = "reported"
    ESTIMATED = "estimated"
    DESIRED = "desired"
    PREDICTED = "predicted"


class FreshnessState(StrEnum):
    """Describe whether the projected property is usable at the current time."""

    FRESH = "fresh"
    DEGRADED = "degraded"
    STALE = "stale"
    UNKNOWN = "unknown"


class TwinEntity(BaseModel):
    """Represent one stable physical, business, or logical shop entity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    twin_id: str = Field(min_length=8, max_length=300)
    tenant_id: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=100)
    entity_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=200)
    model_version: str = Field(default="twin-v1", min_length=1, max_length=40)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None

    @field_validator("twin_id")
    @classmethod
    def require_shelfwise_urn(cls, value: str) -> str:
        """Require namespaced IDs so source identifiers never become global primary keys."""
        if not value.startswith("urn:shelfwise:"):
            raise ValueError("twin_id must start with urn:shelfwise:")
        return value

    @field_validator("created_at", "retired_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous timestamps at the trust boundary."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone")
        return value


class TwinRelationship(BaseModel):
    """Connect two tenant-scoped twin entities without introducing a second graph system."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relationship_id: str = Field(min_length=8, max_length=300)
    tenant_id: str = Field(min_length=1, max_length=100)
    source_twin_id: str = Field(min_length=8, max_length=300)
    relationship_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    target_twin_id: str = Field(min_length=8, max_length=300)
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: datetime | None = None

    @field_validator("valid_from", "valid_to")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous relationship validity timestamps."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone")
        return value


class TwinEntitySeed(BaseModel):
    """Describe one optional entity supplied during exact-store onboarding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")
    entity_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep onboarding metadata bounded and free of raw media fields."""
        _assert_safe_value(value)
        return value


class TwinOnboardingManifest(BaseModel):
    """Bind the operational twin to one named shop and its initial topology."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=80)
    model_version: str = Field(default="twin-v1", min_length=1, max_length=40)
    entities: list[TwinEntitySeed] = Field(default_factory=list, max_length=500)


class TwinSnapshot(BaseModel):
    """Identify one reproducible read model without copying raw source payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    snapshot_id: str
    generated_at: datetime
    entity_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    property_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    projection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TwinObservation(BaseModel):
    """Capture one immutable, provenance-bearing property observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=8, max_length=300)
    tenant_id: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=100)
    twin_id: str = Field(min_length=8, max_length=300)
    property_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,127}$")
    lane: StateLane
    value: Any
    unit: str | None = Field(default=None, max_length=40)
    observed_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_system: str = Field(min_length=1, max_length=80)
    source_object_id: str = Field(min_length=1, max_length=300)
    source_sequence: str | None = Field(default=None, max_length=200)
    source_quality: float = Field(ge=0.0, le=1.0)
    schema_version: str = Field(default="v1", min_length=1, max_length=40)
    correlation_id: str = Field(min_length=1, max_length=300)
    causation_id: str | None = Field(default=None, max_length=300)
    scenario_branch_id: str | None = Field(default=None, max_length=200)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        """Reject timestamps that do not identify a timezone."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("value")
    @classmethod
    def reject_unbounded_or_media_payloads(cls, value: Any) -> Any:
        """Keep observations JSON-safe and structurally free of raw media."""
        _assert_safe_value(value)
        return value

    @model_validator(mode="after")
    def validate_lane_branch(self) -> TwinObservation:
        """Keep scenario predictions isolated from the live reported lane."""
        if self.lane is StateLane.PREDICTED and not self.scenario_branch_id:
            raise ValueError("predicted observations require scenario_branch_id")
        if self.lane is not StateLane.PREDICTED and self.scenario_branch_id:
            raise ValueError("only predicted observations may carry scenario_branch_id")
        return self


class TwinPropertyState(BaseModel):
    """Expose the latest projected value together with trust and provenance metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    twin_id: str
    property_name: str
    lane: StateLane
    value: Any
    unit: str | None
    observation_id: str
    observed_at: datetime
    projected_at: datetime
    source_system: str
    source_quality: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    freshness: FreshnessState
    scenario_branch_id: str | None = None


_SAFE_KEY = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")
_MEDIA_KEY_PARTS = ("image", "video", "frame", "audio", "footage", "biometric", "raw_media")


def _assert_safe_value(value: Any, *, depth: int = 0) -> None:
    """Bound nested observation data and reject raw camera/audio-shaped fields."""
    if depth > 6:
        raise ValueError("observation value is too deeply nested")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 4_000:
            raise ValueError("observation text value is too long")
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("observation list is too large")
        for item in value:
            _assert_safe_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("observation object is too large")
        for key, item in value.items():
            key_text = str(key)
            if not _SAFE_KEY.fullmatch(key_text):
                raise ValueError("observation contains an invalid field name")
            lowered = key_text.lower()
            if any(part in lowered for part in _MEDIA_KEY_PARTS):
                raise ValueError("raw media and biometric fields are not accepted")
            _assert_safe_value(item, depth=depth + 1)
        return
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("observation value must be JSON-compatible") from exc
