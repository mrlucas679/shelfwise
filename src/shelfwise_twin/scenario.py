"""Isolated what-if branches for the exact-store twin.

Scenario state is deliberately kept in the predicted lane.  A branch can therefore be
replayed or discarded without changing reported physical state or re-running the cascade.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import StateLane, TwinObservation
from .scenario_store import InMemoryScenarioBranchStore, ScenarioBranchStore
from .service import TwinService


class ScenarioDelta(BaseModel):
    """One bounded property change proposed inside a scenario branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    twin_id: str = Field(min_length=8, max_length=300)
    property_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,127}$")
    value: Any
    unit: str | None = Field(default=None, max_length=40)


class ScenarioRequest(BaseModel):
    """Create a branch from the current observed store state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{3,120}$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    deltas: list[ScenarioDelta] = Field(default_factory=list, max_length=100)


class ScenarioEngine:
    """Manage durable branch metadata and isolated predicted properties."""

    def __init__(
        self,
        service: TwinService,
        branch_store: ScenarioBranchStore | None = None,
    ) -> None:
        self.service = service
        self.branches = branch_store or InMemoryScenarioBranchStore()

    def create(self, tenant_id: str, store_id: str, request: ScenarioRequest) -> dict[str, Any]:
        """Create and populate one isolated branch; reject duplicate branch identifiers."""
        self._validate_deltas(tenant_id, store_id, request.deltas)
        base = self.service.snapshot(tenant_id, store_id)
        branch = {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "branch_id": request.branch_id,
            "base_snapshot_id": base["snapshot_id"],
            "base_projection_hash": base["projection_hash"],
            "parameters": request.parameters,
            "created_at": datetime.now(UTC).isoformat(),
            "deltas": [],
        }
        self.branches.create(branch)
        try:
            for delta in request.deltas:
                self._apply_delta(branch, delta)
        finally:
            self.branches.update(branch)
        return self.compare(tenant_id, store_id, request.branch_id)

    def compare(self, tenant_id: str, store_id: str, branch_id: str) -> dict[str, Any]:
        """Return observed-versus-predicted values and a reproducible branch hash."""
        branch = self.branches.get(tenant_id, store_id, branch_id)
        if branch is None:
            raise KeyError("scenario branch not found")
        observed = {
            (item.twin_id, item.property_name): item
            for item in self.service.store.list_properties(tenant_id, store_id=store_id)
            if item.lane is StateLane.REPORTED
        }
        predicted = [
            item
            for item in self.service.store.list_properties(tenant_id, store_id=store_id)
            if item.lane is StateLane.PREDICTED and item.scenario_branch_id == branch_id
        ]
        rows = []
        for item in predicted:
            baseline = observed.get((item.twin_id, item.property_name))
            rows.append({
                "twin_id": item.twin_id,
                "property_name": item.property_name,
                "observed": baseline.value if baseline else None,
                "predicted": item.value,
                "unit": item.unit,
                "confidence": item.confidence,
            })
        canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
        # Verify the invariant rather than assert it: no reported-lane property may carry
        # this branch's scenario_branch_id, which would mean a predicted write leaked into
        # observed state instead of staying isolated to the branch.
        reported_state_untouched = all(
            item.scenario_branch_id != branch_id for item in observed.values()
        )
        return {
            "branch": branch,
            "rows": rows,
            "predicted_count": len(rows),
            "branch_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "reported_state_untouched": reported_state_untouched,
        }

    def clear(self) -> None:
        """Forget branch metadata; projected history remains auditable and tenant scoped."""
        self.branches.clear()

    def _validate_deltas(
        self,
        tenant_id: str,
        store_id: str,
        deltas: list[ScenarioDelta],
    ) -> None:
        """Reject the request before reserving metadata or writing predicted state."""
        for delta in deltas:
            entity = self.service.store.get_entity(tenant_id, delta.twin_id)
            if entity is None or entity.store_id != store_id:
                raise ValueError("scenario delta references an unknown store entity")

    def _apply_delta(self, branch: dict[str, Any], delta: ScenarioDelta) -> None:
        """Translate a delta into a deterministic predicted observation."""
        tenant_id = str(branch["tenant_id"])
        store_id = str(branch["store_id"])
        branch_id = str(branch["branch_id"])
        entity = self.service.store.get_entity(tenant_id, delta.twin_id)
        if entity is None or entity.store_id != store_id:
            raise ValueError("scenario delta references an unknown store entity")
        raw = json.dumps(
            {"branch": branch_id, "twin": delta.twin_id, "property": delta.property_name,
             "value": delta.value},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        digest = hashlib.sha256(raw.encode()).hexdigest()
        observation = TwinObservation(
            observation_id=f"scenario_{digest[:32]}", tenant_id=tenant_id, store_id=store_id,
            twin_id=delta.twin_id, property_name=delta.property_name, lane=StateLane.PREDICTED,
            value=delta.value, unit=delta.unit, observed_at=datetime.now(UTC),
            source_system="scenario", source_object_id=f"{branch_id}:{digest[:16]}",
            source_quality=1.0, schema_version="scenario-v1", correlation_id=branch_id,
            scenario_branch_id=branch_id, payload_hash=digest,
        )
        self.service.accept(observation)
        branch["deltas"].append(delta.model_dump(mode="json"))
