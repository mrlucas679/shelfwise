from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import FreshnessState, StateLane, TwinObservation, TwinPropertyState


class TwinProjectionStore(Protocol):
    """Storage operations required by the deterministic projector."""

    def record_observation(self, value: TwinObservation) -> bool: ...

    def get_property(
        self,
        *,
        tenant_id: str,
        twin_id: str,
        property_name: str,
        lane: StateLane,
        scenario_branch_id: str | None,
    ) -> TwinPropertyState | None: ...

    def upsert_property(self, value: TwinPropertyState) -> None: ...


class SourcePolicyResolver(Protocol):
    """Resolve authority, freshness, and reliability for a source/property pair."""

    def __call__(self, property_name: str, source_system: str) -> SourcePolicy: ...


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Control precedence, freshness, and reliability for one source/property."""

    source_system: str
    precedence: int
    max_age: timedelta
    reliability: float


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """Return an auditable projector decision for one observation."""

    status: str
    observation_id: str
    reason: str
    state: TwinPropertyState | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the receipt for API and audit consumers."""
        return {
            "status": self.status,
            "observation_id": self.observation_id,
            "reason": self.reason,
            "state": self.state.model_dump(mode="json") if self.state else None,
        }


class TwinProjector:
    """Materialize current twin properties without mixing lanes or tenants."""

    def __init__(
        self,
        store: TwinProjectionStore,
        policy_for: SourcePolicyResolver | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._policy_for = policy_for or default_source_policy
        self._clock = clock

    def apply(self, observation: TwinObservation) -> ProjectionResult:
        """Record one idempotent observation and project it if it wins authority ordering."""
        if not self._store.record_observation(observation):
            return ProjectionResult("duplicate", observation.observation_id, "already recorded")
        current = self._store.get_property(
            tenant_id=observation.tenant_id,
            twin_id=observation.twin_id,
            property_name=observation.property_name,
            lane=observation.lane,
            scenario_branch_id=observation.scenario_branch_id,
        )
        if current and not self._may_replace(current, observation):
            return ProjectionResult(
                "recorded_not_projected",
                observation.observation_id,
                "older or lower-authority observation",
                current,
            )
        state = self._state(observation)
        self._store.upsert_property(state)
        return ProjectionResult("projected", observation.observation_id, "accepted", state)

    def _may_replace(self, current: TwinPropertyState, incoming: TwinObservation) -> bool:
        """Prefer newer observations, then the explicitly stronger source."""
        if incoming.observed_at > current.observed_at:
            return True
        if incoming.observed_at < current.observed_at:
            return False
        incoming_policy = self._policy_for(incoming.property_name, incoming.source_system)
        current_policy = self._policy_for(current.property_name, current.source_system)
        if incoming_policy.precedence != current_policy.precedence:
            return incoming_policy.precedence < current_policy.precedence
        return incoming.observation_id > current.observation_id

    def _state(self, observation: TwinObservation) -> TwinPropertyState:
        """Calculate bounded confidence while retaining the immutable observation link."""
        now = self._clock()
        policy = self._policy_for(observation.property_name, observation.source_system)
        freshness, freshness_factor = _freshness(now, observation.observed_at, policy.max_age)
        confidence = max(
            0.0,
            min(1.0, policy.reliability * observation.source_quality * freshness_factor),
        )
        return TwinPropertyState(
            tenant_id=observation.tenant_id,
            twin_id=observation.twin_id,
            property_name=observation.property_name,
            lane=observation.lane,
            value=observation.value,
            unit=observation.unit,
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
            projected_at=now,
            source_system=observation.source_system,
            source_quality=observation.source_quality,
            confidence=confidence,
            freshness=freshness,
            scenario_branch_id=observation.scenario_branch_id,
        )


def default_source_policy(property_name: str, source_system: str) -> SourcePolicy:
    """Return conservative defaults until a tenant-specific authority policy is configured."""
    del property_name
    normalized = source_system.strip().lower()
    policies = {
        "api": SourcePolicy(normalized, 10, timedelta(hours=1), 0.95),
        "manual": SourcePolicy(normalized, 15, timedelta(hours=4), 0.90),
        "scanner": SourcePolicy(normalized, 20, timedelta(hours=12), 0.90),
        "pos_csv": SourcePolicy(normalized, 30, timedelta(hours=24), 0.88),
        "wms_csv": SourcePolicy(normalized, 30, timedelta(hours=12), 0.88),
    }
    return policies.get(
        normalized,
        SourcePolicy(normalized or "unknown", 50, timedelta(hours=24), 0.75),
    )


def _freshness(
    now: datetime,
    observed_at: datetime,
    max_age: timedelta,
) -> tuple[FreshnessState, float]:
    """Convert age into an explicit state and bounded confidence factor."""
    age = max(timedelta(0), now - observed_at)
    if age <= max_age:
        return FreshnessState.FRESH, 1.0
    if age <= max_age * 2:
        return FreshnessState.DEGRADED, 0.6
    return FreshnessState.STALE, 0.0
