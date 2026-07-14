from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Event, EventType
from shelfwise_runtime.provenance import DataDomain, DataDomainBoundaryError

from .calibration import CalibrationRecord, CalibrationRegistry, create_calibration_registry
from .fidelity import FidelityInputs, calculate_fidelity
from .models import (
    StateLane,
    TwinEntity,
    TwinObservation,
    TwinOnboardingManifest,
    TwinRelationship,
    TwinSnapshot,
)
from .onboarding_store import OnboardingManifestRegistry, create_onboarding_manifest_registry
from .projector import ProjectionResult, TwinProjector
from .store import TwinStore, create_twin_store


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """Describe one safe, scalar property extracted from a canonical event."""

    twin_id: str
    entity_type: str
    display_name: str
    property_name: str
    value: Any
    unit: str | None = None


class TwinService:
    """Own twin topology, event-to-observation translation, projection, and read models."""

    def __init__(
        self,
        store: TwinStore,
        *,
        calibrations: CalibrationRegistry | None = None,
        onboarding_manifests: OnboardingManifestRegistry | None = None,
    ) -> None:
        self.store = store
        self.projector = TwinProjector(store)
        self.calibrations = (
            calibrations if calibrations is not None else create_calibration_registry()
        )
        self.onboarding_manifests = (
            onboarding_manifests
            if onboarding_manifests is not None
            else create_onboarding_manifest_registry()
        )

    def accept(self, observation: TwinObservation) -> ProjectionResult:
        """Accept one validated observation and create its minimal topology if needed."""
        entity = self.store.get_entity(observation.tenant_id, observation.twin_id)
        if entity is None:
            self._ensure_entity_for_observation(observation)
        return self.projector.apply(observation)

    def project_event(self, event: Event) -> list[ProjectionResult]:
        """Translate one canonical event into idempotent, non-media twin observations."""
        if event.data_domain is not DataDomain.OPERATIONAL_TWIN:
            raise DataDomainBoundaryError(
                boundary="operational twin projection",
                actual=event.data_domain,
                expected=DataDomain.OPERATIONAL_TWIN,
            )
        store_id = _store_id(event.payload)
        if not store_id:
            return []
        store_entity = self._ensure_entity(
            tenant_id=event.tenant_id,
            store_id=store_id,
            entity_type="store",
            local_id=store_id,
            display_name=f"Store {store_id}",
            created_at=event.ts,
        )
        specs = _event_specs(event, store_id=store_id)
        results: list[ProjectionResult] = []
        for spec in specs:
            entity = self._ensure_entity(
                tenant_id=event.tenant_id,
                store_id=store_id,
                entity_type=spec.entity_type,
                local_id=_local_id(spec.twin_id),
                display_name=spec.display_name,
                created_at=event.ts,
            )
            self._link(
                tenant_id=event.tenant_id,
                source=store_entity.twin_id,
                target=entity.twin_id,
                relationship_type="contains",
                event_ts=event.ts,
            )
            observation = _observation_for_spec(event, spec, entity.twin_id, store_id)
            results.append(self.projector.apply(observation))
        return results

    def onboard(self, manifest: TwinOnboardingManifest) -> dict[str, Any]:
        """Create an explicit store root and seed topology before live synchronization.

        Persists the manifest durably (`onboarding_manifests`), separately from the twin
        projection store, so `bootstrap_events` can re-apply it if the projection is ever lost
        and rebuilt - onboarding topology is not represented in the replayable event log.
        """
        self.onboarding_manifests.save(manifest)
        root = self._ensure_entity(
            tenant_id=manifest.tenant_id,
            store_id=manifest.store_id,
            entity_type="store",
            local_id=manifest.store_id,
            display_name=manifest.display_name,
            created_at=datetime.now(UTC),
            model_version=manifest.model_version,
            attributes={"timezone": manifest.timezone, "onboarding": "explicit"},
        )
        for seed in manifest.entities:
            entity = self._ensure_entity(
                tenant_id=manifest.tenant_id,
                store_id=manifest.store_id,
                entity_type=seed.entity_type,
                local_id=seed.local_id,
                display_name=seed.display_name,
                created_at=root.created_at,
                model_version=manifest.model_version,
                attributes=seed.attributes,
            )
            self._link(
                tenant_id=manifest.tenant_id,
                source=root.twin_id,
                target=entity.twin_id,
                relationship_type="contains",
                event_ts=root.created_at,
            )
        return {
            "manifest": manifest.model_dump(mode="json"),
            "snapshot": self.snapshot(
                manifest.tenant_id,
                manifest.store_id,
                generated_at=root.created_at,
            ),
        }

    def bootstrap_events(
        self,
        events: list[Event],
        *,
        tenant_id: str,
        store_id: str,
    ) -> dict[str, Any]:
        """Replay existing canonical events into the twin without re-running cascades.

        Re-applies the durably saved onboarding manifest (if any) first, so a rebuilt
        projection restores seeded topology (fixtures, the onboarded store display
        name/attributes) rather than only whatever entities the operational events imply.
        """
        manifest = self.onboarding_manifests.get(tenant_id, store_id)
        if manifest is not None:
            self.onboard(manifest)
        relevant = [
            event
            for event in events
            if event.tenant_id == tenant_id and _store_id(event.payload) == store_id
        ]
        operational = [
            event
            for event in relevant
            if event.data_domain is DataDomain.OPERATIONAL_TWIN
        ]
        ordered = sorted(
            operational,
            key=lambda event: (event.ts, event.id),
        )
        receipts = [result for event in ordered for result in self.project_event(event)]
        return {
            "store_id": store_id,
            "events_considered": len(relevant),
            "events_operational": len(ordered),
            "events_skipped_non_operational": len(relevant) - len(ordered),
            "observations_processed": len(receipts),
            "projected": sum(result.status == "projected" for result in receipts),
            "duplicates": sum(result.status == "duplicate" for result in receipts),
            "snapshot": self.snapshot(tenant_id, store_id),
        }

    def snapshot(
        self,
        tenant_id: str,
        store_id: str,
        *,
        generated_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic projection receipt for replay, recovery, and assistant context."""
        entities = self.store.list_entities(tenant_id, store_id=store_id)
        relationships = self.store.list_relationships(tenant_id, store_id=store_id)
        properties = self.store.list_properties(tenant_id, store_id=store_id)
        observations = self.store.list_observations(tenant_id, store_id=store_id, limit=500)
        # created_at/valid_from/projected_at/confidence/freshness are stamped from wall-clock
        # "now" at projection time (see TwinEntity.created_at, TwinRelationship.valid_from's
        # default, and TwinProjector._state) - and onboarding manifest replay re-derives
        # created_at/valid_from afresh each time it re-applies (see TwinService.onboard). Two
        # projections of the identical event log/manifest at different real times would
        # otherwise never hash equal. Exclude these wall-clock-derived fields so the hash
        # reflects substantive twin state only - the property needed for replay/recovery
        # verification (rebuilding from the same durable events/manifest must reproduce the
        # same hash).
        canonical = json.dumps(
            {
                "entities": [
                    item.model_dump(mode="json", exclude={"created_at"}) for item in entities
                ],
                "relationships": [
                    item.model_dump(mode="json", exclude={"valid_from"})
                    for item in relationships
                ],
                "properties": [
                    item.model_dump(
                        mode="json", exclude={"projected_at", "confidence", "freshness"}
                    )
                    for item in properties
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        projection_hash = hashlib.sha256(canonical.encode()).hexdigest()
        timestamp = generated_at or datetime.now(UTC)
        snapshot = TwinSnapshot(
            tenant_id=tenant_id,
            store_id=store_id,
            snapshot_id=f"snapshot_{projection_hash[:24]}",
            generated_at=timestamp,
            entity_count=len(entities),
            relationship_count=len(relationships),
            property_count=len(properties),
            observation_count=len(observations),
            projection_hash=projection_hash,
        )
        return snapshot.model_dump(mode="json")

    def get_store(self, tenant_id: str, store_id: str, *, limit: int = 200) -> dict[str, Any]:
        """Return a tenant-scoped topology, current state, recent history, and fidelity."""
        entities = self.store.list_entities(tenant_id, store_id=store_id)
        return {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "entities": [item.model_dump(mode="json") for item in entities],
            "relationships": [
                item.model_dump(mode="json")
                for item in self.store.list_relationships(tenant_id, store_id=store_id)
            ],
            "properties": [
                item.model_dump(mode="json")
                for item in self.store.list_properties(tenant_id, store_id=store_id)
            ],
            "observations": [
                item.model_dump(mode="json")
                for item in self.store.list_observations(tenant_id, store_id=store_id, limit=limit)
            ],
            "fidelity": self.fidelity(tenant_id, store_id),
        }

    def live_context(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        property_name: str | None = None,
        entity_local_id: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        """Return only reported operational state for live assistant grounding."""
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        properties = [
            item
            for item in self.store.list_properties(tenant_id, store_id=store_id)
            if item.lane is StateLane.REPORTED
            and (property_name is None or item.property_name == property_name)
            and (
                entity_local_id is None
                or _local_id(item.twin_id) == _slug(entity_local_id)
            )
        ][:limit]
        return {
            "data_domain": DataDomain.OPERATIONAL_TWIN.value,
            "synthetic": False,
            "authoritative_scope": "reported_operational_observations_only",
            "store_id": store_id,
            "properties": [item.model_dump(mode="json") for item in properties],
            "source_refs": [item.observation_id for item in properties],
            "missing_data": [] if properties else ["reported twin observations"],
        }

    def get_entity(self, tenant_id: str, twin_id: str) -> dict[str, Any] | None:
        """Return one entity and every tenant-scoped edge/property attached to it."""
        entity = self.store.get_entity(tenant_id, twin_id)
        if entity is None:
            return None
        properties = [
            item for item in self.store.list_properties(tenant_id) if item.twin_id == twin_id
        ]
        observations = [
            item
            for item in self.store.list_observations(tenant_id, limit=500)
            if item.twin_id == twin_id
        ]
        relationships = [
            item
            for item in self.store.list_relationships(tenant_id)
            if item.source_twin_id == twin_id or item.target_twin_id == twin_id
        ]
        return {
            "entity": entity.model_dump(mode="json"),
            "properties": [item.model_dump(mode="json") for item in properties],
            "observations": [item.model_dump(mode="json") for item in observations],
            "relationships": [item.model_dump(mode="json") for item in relationships],
        }

    def fidelity(
        self,
        tenant_id: str,
        store_id: str,
        *,
        expected_device_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Calculate an honest, dimensioned readiness score for one store projection.

        `expected_device_ids` (the store's provisioned edge devices, if any are registered)
        lets the caller assert calibration coverage against the actual device population
        instead of a bare "at least one calibration exists" check, which would trivially
        pass after a single throwaway reading.
        """
        entities = self.store.list_entities(tenant_id, store_id=store_id)
        relationships = self.store.list_relationships(tenant_id, store_id=store_id)
        observations = self.store.list_observations(tenant_id, store_id=store_id, limit=500)
        properties = self.store.list_properties(tenant_id, store_id=store_id)
        entity_count = len(entities)
        observation_count = len(observations)
        source_agreement = _source_agreement(observations)
        fresh_ratio = (
            sum(1 for item in properties if item.freshness.value == "fresh") / len(properties)
            if properties
            else 0.0
        )
        score = calculate_fidelity(
            FidelityInputs(
                identity_coverage=1.0
                if any(item.entity_type == "store" for item in entities)
                else 0.0,
                topology_coverage=(
                    min(1.0, len(relationships) / max(entity_count - 1, 1))
                    if entity_count > 1
                    else 0.0
                ),
                fresh_property_ratio=fresh_ratio,
                provenance_ratio=(
                    sum(1 for item in observations if item.source_object_id) / observation_count
                    if observation_count
                    else 0.0
                ),
                source_agreement=source_agreement,
                projection_health=(
                    min(1.0, len(properties) / observation_count) if observation_count else 0.0
                ),
                calibration_score=self.calibrations.score(tenant_id, store_id),
            )
        )
        return {
            **score.to_dict(),
            "tenant_id": tenant_id,
            "store_id": store_id,
            "entities": entity_count,
            "relationships": len(relationships),
            "observations": observation_count,
            "properties": len(properties),
            "hard_guards": {
                "no_raw_media": True,
                "tenant_scoped": True,
                "lane_isolation": True,
                "calibration_complete": _calibration_complete(
                    expected_device_ids, self.calibrations.list(tenant_id, store_id)
                ),
            },
        }

    def _ensure_entity_for_observation(self, observation: TwinObservation) -> TwinEntity:
        """Create a minimal entity for authenticated edge/API observations."""
        return self._ensure_entity(
            tenant_id=observation.tenant_id,
            store_id=observation.store_id,
            entity_type="observed_entity",
            local_id=observation.twin_id.rsplit(":", 1)[-1],
            display_name=observation.twin_id.rsplit(":", 1)[-1],
            created_at=observation.observed_at,
            twin_id=observation.twin_id,
        )

    def _ensure_entity(
        self,
        *,
        tenant_id: str,
        store_id: str,
        entity_type: str,
        local_id: str,
        display_name: str,
        created_at: datetime,
        twin_id: str | None = None,
        model_version: str = "twin-v1",
        attributes: dict[str, Any] | None = None,
    ) -> TwinEntity:
        """Create or read one stable namespaced entity."""
        entity = TwinEntity(
            tenant_id=tenant_id,
            store_id=store_id,
            twin_id=twin_id or _urn(tenant_id, store_id, entity_type, local_id),
            entity_type=entity_type,
            display_name=display_name[:200],
            model_version=model_version,
            attributes=attributes or {},
            created_at=created_at,
        )
        return self.store.ensure_entity(entity)

    def _link(
        self,
        *,
        tenant_id: str,
        source: str,
        target: str,
        relationship_type: str,
        event_ts: datetime,
    ) -> None:
        """Add a deterministic edge once, preserving topology continuity."""
        relationship_id = (
            "rel_"
            + hashlib.sha256(
                f"{tenant_id}|{source}|{relationship_type}|{target}".encode()
            ).hexdigest()[:24]
        )
        self.store.add_relationship(
            TwinRelationship(
                relationship_id=relationship_id,
                tenant_id=tenant_id,
                source_twin_id=source,
                relationship_type=relationship_type,
                target_twin_id=target,
                valid_from=event_ts,
            )
        )


def create_twin_service() -> TwinService:
    """Build the service using the application's configured state backend."""
    return TwinService(create_twin_store())


def _event_specs(event: Event, *, store_id: str) -> list[ObservationSpec]:
    """Map supported canonical event types to safe, scalar twin properties."""
    payload = event.payload
    sku = _text(payload.get("sku"))
    subject_id = sku or _text(payload.get("asset_id")) or _text(payload.get("supplier"))
    if not subject_id:
        subject_id = store_id
    product_name = _text(payload.get("product")) or f"Entity {subject_id}"
    product_twin_id = _urn(event.tenant_id, store_id, "product", sku) if sku else None
    twin_id = product_twin_id or _urn(event.tenant_id, store_id, "asset", subject_id)
    specs: list[ObservationSpec] = []
    if event.type is EventType.STOCK_UPDATE:
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "inventory.on_hand",
            payload.get("on_hand", payload.get("quantity")),
            "units",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "inventory.reorder_point",
            payload.get("reorder_point"),
            "units",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "inventory.stock_state",
            payload.get("stock_state"),
        )
        _append_if_present(
            specs, twin_id, "product", product_name, "catalog.category", payload.get("category")
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "catalog.unit_cost_minor_units",
            _money_minor_units(
                payload.get("unit_cost"),
                payload.get("unit_cost_cents", payload.get("unit_cost_minor_units")),
            ),
            "ZAR cents",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "catalog.unit_price_minor_units",
            _money_minor_units(
                payload.get("unit_price"),
                payload.get("catalog_price_cents", payload.get("unit_price_minor_units")),
            ),
            "ZAR cents",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "sourcing.supplier_id",
            payload.get("supplier", payload.get("supplier_id")),
        )
    elif event.type is EventType.SALE:
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "sales.units",
            payload.get("units", payload.get("quantity")),
            "units",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "sales.till_price_cents",
            payload.get("unit_price_cents", payload.get("unit_price_minor_units")),
            "ZAR cents",
        )
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "sales.catalog_price_cents",
            payload.get("catalog_price_cents"),
            "ZAR cents",
        )
        _append_if_present(
            specs, twin_id, "product", product_name, "catalog.category", payload.get("category")
        )
    elif event.type is EventType.SCAN:
        _append_if_present(specs, twin_id, "product", product_name, "scan.sku", sku)
        _append_if_present(
            specs, twin_id, "product", product_name, "scan.barcode", payload.get("barcode")
        )
    elif event.type is EventType.EXPIRY_ENTRY:
        _append_if_present(
            specs,
            twin_id,
            "product",
            product_name,
            "expiry.days_to_expiry",
            payload.get("days_to_expiry"),
            "days",
        )
        _append_if_present(
            specs, twin_id, "product", product_name, "expiry.batch_id", payload.get("batch_id")
        )
        _append_if_present(
            specs, twin_id, "product", product_name, "expiry.storage", payload.get("storage")
        )
    elif event.type is EventType.COLD_CHAIN_ALERT:
        _append_if_present(
            specs, twin_id, "asset", product_name, "cold_chain.diagnosis", payload.get("diagnosis")
        )
        _append_if_present(
            specs, twin_id, "asset", product_name, "cold_chain.severity", payload.get("severity")
        )
        _append_if_present(
            specs,
            twin_id,
            "asset",
            product_name,
            "cold_chain.predicted_minutes_to_unsafe",
            payload.get("predicted_minutes_to_unsafe"),
            "minutes",
        )
        _append_if_present(
            specs,
            twin_id,
            "asset",
            product_name,
            "cold_chain.measured_outage_hours",
            payload.get("measured_outage_hours"),
            "hours",
        )
        _append_if_present(
            specs,
            twin_id,
            "asset",
            product_name,
            "cold_chain.average_temp_c",
            payload.get("temp_c", payload.get("average_temp_c")),
            "degC",
        )
        risk = payload.get("stock_at_risk")
        if isinstance(risk, dict):
            _append_if_present(
                specs,
                twin_id,
                "asset",
                product_name,
                "cold_chain.stock_at_risk_minor_units",
                risk.get("minor_units"),
                "ZAR cents",
            )
    elif event.type is EventType.SHIPMENT:
        order_id = _text(payload.get("order_id")) or event.id
        twin_id = _urn(event.tenant_id, store_id, "order", order_id)
        _append_if_present(
            specs,
            twin_id,
            "order",
            f"Order {order_id}",
            "logistics.ordered_units",
            payload.get("ordered_units"),
            "units",
        )
        _append_if_present(
            specs, twin_id, "order", f"Order {order_id}", "logistics.eta", payload.get("eta")
        )
    elif event.type is EventType.SUPPLIER_UPDATE:
        supplier = _text(payload.get("supplier")) or "unknown"
        twin_id = _urn(event.tenant_id, store_id, "supplier", supplier)
        _append_if_present(
            specs,
            twin_id,
            "supplier",
            supplier,
            "supplier.lead_time_days",
            payload.get("lead_time_days", payload.get("avg_lead_time_days")),
            "days",
        )
        _append_if_present(
            specs,
            twin_id,
            "supplier",
            supplier,
            "supplier.recent_delay",
            payload.get("recent_delay"),
        )
        _append_if_present(
            specs,
            twin_id,
            "supplier",
            supplier,
            "supplier.fill_rate",
            payload.get("fill_rate"),
        )
        _append_if_present(
            specs,
            twin_id,
            "supplier",
            supplier,
            "supplier.available_units",
            payload.get("available_units"),
            "units",
        )
        _append_if_present(
            specs,
            twin_id,
            "supplier",
            supplier,
            "supplier.distance_km",
            payload.get("distance_km"),
            "km",
        )
        if product_twin_id is not None:
            _append_if_present(
                specs,
                product_twin_id,
                "product",
                product_name,
                "sourcing.supplier_id",
                supplier,
            )
    elif event.type in {EventType.RECALL_NOTICE, EventType.INVENTORY_EXCEPTION}:
        prefix = (
            "quality.recall" if event.type is EventType.RECALL_NOTICE else "inventory.exception"
        )
        _append_if_present(
            specs,
            twin_id,
            "product" if sku else "store",
            product_name,
            f"{prefix}.status",
            "active",
        )
        _append_if_present(
            specs,
            twin_id,
            "product" if sku else "store",
            product_name,
            f"{prefix}.detail",
            payload.get("reason", payload.get("notice")),
        )
    return specs


def _observation_for_spec(
    event: Event, spec: ObservationSpec, twin_id: str, store_id: str
) -> TwinObservation:
    """Create a deterministic observation ID and content hash for one event property."""
    canonical = json.dumps(
        {"event_id": event.id, "property": spec.property_name, "value": _json_value(spec.value)},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return TwinObservation(
        observation_id=f"obs_{digest[:32]}",
        tenant_id=event.tenant_id,
        store_id=store_id,
        twin_id=twin_id,
        property_name=spec.property_name,
        lane=StateLane.REPORTED,
        value=_json_value(spec.value),
        unit=spec.unit,
        observed_at=event.ts,
        source_system=event.source.value,
        source_object_id=event.id,
        source_sequence=_text(event.payload.get("sequence")),
        source_quality=_source_quality(event.payload),
        schema_version=event.schema_version,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        payload_hash=digest,
    )


def _append_if_present(
    specs: list[ObservationSpec],
    twin_id: str,
    entity_type: str,
    display_name: str,
    property_name: str,
    value: Any,
    unit: str | None = None,
) -> None:
    """Keep absent optional event fields out of the authoritative twin."""
    if value is not None:
        specs.append(
            ObservationSpec(twin_id, entity_type, display_name, property_name, value, unit)
        )


def _store_id(payload: dict[str, Any]) -> str | None:
    """Resolve a store identifier from canonical source aliases."""
    for key in ("store_id", "site_id", "location", "location_id"):
        value = _text(payload.get(key))
        if value:
            return value
    return None


def _urn(tenant_id: str, store_id: str, entity_type: str, local_id: str) -> str:
    """Build a stable, sanitized ShelfWise URN."""
    return "urn:shelfwise:" + ":".join(
        _slug(part) for part in (tenant_id, store_id, entity_type, local_id)
    )


def _local_id(twin_id: str) -> str:
    """Return the final URN segment for entity creation."""
    return twin_id.rsplit(":", 1)[-1]


def _slug(value: Any) -> str:
    """Keep source IDs readable while preventing path/control-character injection."""
    result = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value).strip())
    return result[:120] or "unknown"


def _text(value: Any) -> str | None:
    """Normalize optional source identifiers without accepting blank strings."""
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _json_value(value: Any) -> Any:
    """Convert event values into bounded JSON primitives before model validation."""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, (Decimal, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    return str(value)


def _money_minor_units(amount: Any, explicit_minor_units: Any) -> int | None:
    """Normalize supported money shapes before projecting one scalar property."""
    if explicit_minor_units is not None:
        try:
            return int(explicit_minor_units)
        except (TypeError, ValueError):
            return None
    if isinstance(amount, dict):
        try:
            return int(amount["minor_units"]) if "minor_units" in amount else None
        except (TypeError, ValueError):
            return None
    if amount is None:
        return None
    try:
        return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _source_quality(payload: dict[str, Any]) -> float:
    """Read an optional source quality hint while failing closed on malformed values."""
    try:
        value = float(payload.get("source_quality", 1.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _calibration_complete(
    expected_device_ids: frozenset[str] | None,
    records: list[CalibrationRecord],
) -> bool:
    """Report completeness against the store's actual device population, not a bare
    "any calibration exists" check that a single throwaway reading would satisfy."""
    if not expected_device_ids:
        return False
    calibrated = {record.device_id for record in records}
    return expected_device_ids <= calibrated


def _source_agreement(observations: list[TwinObservation]) -> float:
    """Penalize contradictory same-property values while keeping source disagreement visible."""
    groups: dict[tuple[str, str], set[str]] = {}
    for observation in observations:
        key = (observation.twin_id, observation.property_name)
        groups.setdefault(key, set()).add(
            json.dumps(observation.value, sort_keys=True, default=str)
        )
    conflicts = sum(1 for values in groups.values() if len(values) > 1)
    return 1.0 / (1.0 + conflicts)
