from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol

from shelfwise_storage import (
    auto_schema_enabled,
    connect,
    jsonb,
)
from shelfwise_storage import (
    validate_limit as _validate_limit,
)
from shelfwise_storage.rls import apply_tenant_rls

from .models import (
    FreshnessState,
    StateLane,
    TwinEntity,
    TwinObservation,
    TwinPropertyState,
    TwinRelationship,
)


class TwinStore(Protocol):
    """Storage contract shared by the disposable local and durable Postgres runtimes."""

    def ensure_entity(self, entity: TwinEntity) -> TwinEntity: ...

    def get_entity(self, tenant_id: str, twin_id: str) -> TwinEntity | None: ...

    def list_entities(self, tenant_id: str, *, store_id: str | None = None) -> list[TwinEntity]: ...

    def add_relationship(self, relationship: TwinRelationship) -> TwinRelationship: ...

    def list_relationships(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinRelationship]: ...

    def record_observation(self, value: TwinObservation) -> bool: ...

    def list_observations(
        self, tenant_id: str, *, store_id: str | None = None, limit: int = 200
    ) -> list[TwinObservation]: ...

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

    def list_properties(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinPropertyState]: ...

    def clear(self) -> None: ...


class InMemoryTwinStore:
    """Tenant-isolated projection store for local development and deterministic tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entities: dict[tuple[str, str], TwinEntity] = {}
        self._relationships: dict[tuple[str, str], TwinRelationship] = {}
        self._observations: dict[tuple[str, str], TwinObservation] = {}
        self._observation_dedupe: set[tuple[str, str, str, str, str, str]] = set()
        self._properties: dict[tuple[str, str, str, str, str], TwinPropertyState] = {}

    def ensure_entity(self, entity: TwinEntity) -> TwinEntity:
        """Create an entity once and preserve its original identity/provenance."""
        key = (entity.tenant_id, entity.twin_id)
        with self._lock:
            existing = self._entities.setdefault(key, entity)
            return deepcopy(existing)

    def get_entity(self, tenant_id: str, twin_id: str) -> TwinEntity | None:
        """Return one entity only when it belongs to the requested tenant."""
        with self._lock:
            entity = self._entities.get((tenant_id, twin_id))
        return deepcopy(entity) if entity else None

    def list_entities(self, tenant_id: str, *, store_id: str | None = None) -> list[TwinEntity]:
        """Return bounded topology rows for one tenant and optional store."""
        with self._lock:
            rows = [
                entity
                for (row_tenant, _), entity in self._entities.items()
                if row_tenant == tenant_id and (store_id is None or entity.store_id == store_id)
            ]
        return deepcopy(sorted(rows, key=lambda item: item.twin_id))

    def add_relationship(self, relationship: TwinRelationship) -> TwinRelationship:
        """Insert one relationship idempotently within the tenant graph."""
        key = (relationship.tenant_id, relationship.relationship_id)
        with self._lock:
            existing = self._relationships.setdefault(key, relationship)
            return deepcopy(existing)

    def list_relationships(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinRelationship]:
        """Return relationships whose source or target is in the requested store."""
        with self._lock:
            rows = [
                relation
                for (row_tenant, _), relation in self._relationships.items()
                if row_tenant == tenant_id
                and (
                    store_id is None
                    or self._entity_in_store(relation.source_twin_id, tenant_id, store_id)
                    or self._entity_in_store(relation.target_twin_id, tenant_id, store_id)
                )
            ]
        return deepcopy(sorted(rows, key=lambda item: item.relationship_id))

    def record_observation(self, value: TwinObservation) -> bool:
        """Append an observation only once by id or source payload identity."""
        key = (value.tenant_id, value.observation_id)
        dedupe_key = (
            value.tenant_id,
            value.source_system,
            value.source_object_id,
            value.property_name,
            value.lane.value,
            value.payload_hash,
        )
        with self._lock:
            if key in self._observations or dedupe_key in self._observation_dedupe:
                return False
            self._observations[key] = value
            self._observation_dedupe.add(dedupe_key)
            return True

    def list_observations(
        self, tenant_id: str, *, store_id: str | None = None, limit: int = 200
    ) -> list[TwinObservation]:
        """Return newest immutable observations for one tenant."""
        _validate_limit(limit)
        with self._lock:
            rows = [
                observation
                for (row_tenant, _), observation in self._observations.items()
                if row_tenant == tenant_id
                and (store_id is None or observation.store_id == store_id)
            ]
        rows.sort(key=lambda item: (item.observed_at, item.observation_id), reverse=True)
        return deepcopy(rows[:limit])

    def get_property(
        self,
        *,
        tenant_id: str,
        twin_id: str,
        property_name: str,
        lane: StateLane,
        scenario_branch_id: str | None,
    ) -> TwinPropertyState | None:
        """Read one current lane/branch without allowing cross-tenant access."""
        key = _property_key(tenant_id, twin_id, property_name, lane, scenario_branch_id)
        with self._lock:
            value = self._properties.get(key)
        return deepcopy(value) if value else None

    def upsert_property(self, value: TwinPropertyState) -> None:
        """Replace only the current state for one tenant/entity/property lane."""
        key = _property_key(
            value.tenant_id,
            value.twin_id,
            value.property_name,
            value.lane,
            value.scenario_branch_id,
        )
        with self._lock:
            self._properties[key] = value

    def list_properties(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinPropertyState]:
        """Return current projected properties, optionally narrowed to a store."""
        with self._lock:
            rows = [
                value
                for (row_tenant, twin_id, _, _, _), value in self._properties.items()
                if row_tenant == tenant_id
                and (store_id is None or self._entity_in_store(twin_id, tenant_id, store_id))
            ]
        return deepcopy(sorted(rows, key=lambda item: (item.twin_id, item.property_name)))

    def clear(self) -> None:
        """Clear the disposable projection state."""
        with self._lock:
            self._entities.clear()
            self._relationships.clear()
            self._observations.clear()
            self._observation_dedupe.clear()
            self._properties.clear()

    def _entity_in_store(self, twin_id: str, tenant_id: str, store_id: str) -> bool:
        entity = self._entities.get((tenant_id, twin_id))
        return entity is not None and entity.store_id == store_id


class PostgresTwinStore:
    """Durable digital-twin projection store protected by tenant RLS."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresTwinStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def ensure_entity(self, entity: TwinEntity) -> TwinEntity:
        """Insert an entity idempotently, then return the tenant-scoped row."""
        with self._connect(entity.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_twin_entities
                    (tenant_id, twin_id, store_id, entity_type, model_version, display_name,
                     attributes, created_at, retired_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, twin_id) do nothing
                """,
                (
                    entity.tenant_id,
                    entity.twin_id,
                    entity.store_id,
                    entity.entity_type,
                    entity.model_version,
                    entity.display_name,
                    jsonb(entity.attributes),
                    entity.created_at,
                    entity.retired_at,
                ),
            )
            conn.commit()
        return self.get_entity(entity.tenant_id, entity.twin_id) or entity

    def get_entity(self, tenant_id: str, twin_id: str) -> TwinEntity | None:
        """Read one entity through the tenant-bound connection."""
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select tenant_id, twin_id, store_id, entity_type, model_version, display_name,
                       attributes, created_at, retired_at
                from shelfwise_twin_entities
                where tenant_id = %s and twin_id = %s
                """,
                (tenant_id, twin_id),
            ).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, tenant_id: str, *, store_id: str | None = None) -> list[TwinEntity]:
        """List tenant topology rows without exposing another store."""
        query = """
            select tenant_id, twin_id, store_id, entity_type, model_version, display_name,
                   attributes, created_at, retired_at
            from shelfwise_twin_entities where tenant_id = %s
        """
        params: list[Any] = [tenant_id]
        if store_id is not None:
            query += " and store_id = %s"
            params.append(store_id)
        query += " order by twin_id"
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_entity_from_row(row) for row in rows]

    def add_relationship(self, relationship: TwinRelationship) -> TwinRelationship:
        """Insert one relationship once and return the canonical row."""
        with self._connect(relationship.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_twin_relationships
                    (tenant_id, relationship_id, source_twin_id, relationship_type,
                     target_twin_id, attributes, valid_from, valid_to)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, relationship_id) do nothing
                """,
                (
                    relationship.tenant_id,
                    relationship.relationship_id,
                    relationship.source_twin_id,
                    relationship.relationship_type,
                    relationship.target_twin_id,
                    jsonb(relationship.attributes),
                    relationship.valid_from,
                    relationship.valid_to,
                ),
            )
            conn.commit()
        return relationship

    def list_relationships(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinRelationship]:
        """List tenant relationships and optionally filter by either endpoint's store."""
        query = """
            select r.tenant_id, r.relationship_id, r.source_twin_id, r.relationship_type,
                   r.target_twin_id, r.attributes, r.valid_from, r.valid_to
            from shelfwise_twin_relationships r
        """
        params: list[Any] = [tenant_id]
        if store_id is not None:
            query += """
                join shelfwise_twin_entities s on s.tenant_id = r.tenant_id
                  and s.twin_id = r.source_twin_id
                left join shelfwise_twin_entities t on t.tenant_id = r.tenant_id
                  and t.twin_id = r.target_twin_id
            """
        query += " where r.tenant_id = %s"
        if store_id is not None:
            query += " and (s.store_id = %s or t.store_id = %s)"
            params.extend([store_id, store_id])
        query += " order by r.relationship_id"
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_relationship_from_row(row) for row in rows]

    def record_observation(self, value: TwinObservation) -> bool:
        """Append an observation idempotently by ID and source payload hash."""
        with self._connect(value.tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_twin_observations
                    (tenant_id, observation_id, store_id, twin_id, property_name, lane, value,
                     unit, observed_at, ingested_at, source_system, source_object_id,
                     source_sequence, source_quality, schema_version, correlation_id,
                     causation_id, scenario_branch_id, payload_hash)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                on conflict do nothing returning observation_id
                """,
                (
                    value.tenant_id,
                    value.observation_id,
                    value.store_id,
                    value.twin_id,
                    value.property_name,
                    value.lane.value,
                    jsonb(value.value),
                    value.unit,
                    value.observed_at,
                    value.ingested_at,
                    value.source_system,
                    value.source_object_id,
                    value.source_sequence,
                    value.source_quality,
                    value.schema_version,
                    value.correlation_id,
                    value.causation_id,
                    value.scenario_branch_id,
                    value.payload_hash,
                ),
            ).fetchone()
            conn.commit()
        return row is not None

    def list_observations(
        self, tenant_id: str, *, store_id: str | None = None, limit: int = 200
    ) -> list[TwinObservation]:
        """Return newest immutable observations for the tenant."""
        _validate_limit(limit)
        query = """
            select observation_id, tenant_id, store_id, twin_id, property_name, lane, value,
                   unit, observed_at, ingested_at, source_system, source_object_id,
                   source_sequence, source_quality, schema_version, correlation_id,
                   causation_id, scenario_branch_id, payload_hash
            from shelfwise_twin_observations where tenant_id = %s
        """
        params: list[Any] = [tenant_id]
        if store_id is not None:
            query += " and store_id = %s"
            params.append(store_id)
        query += " order by observed_at desc, observation_id desc limit %s"
        params.append(limit)
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_observation_from_row(row) for row in rows]

    def get_property(
        self,
        *,
        tenant_id: str,
        twin_id: str,
        property_name: str,
        lane: StateLane,
        scenario_branch_id: str | None,
    ) -> TwinPropertyState | None:
        """Read one tenant/entity/property lane from the current-state projection."""
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select tenant_id, twin_id, property_name, lane, value, unit, observation_id,
                       observed_at, projected_at, source_system, source_quality, confidence,
                       freshness, scenario_branch_key
                from shelfwise_twin_property_state
                where tenant_id = %s and twin_id = %s and property_name = %s
                  and lane = %s and scenario_branch_key = %s
                """,
                (
                    tenant_id,
                    twin_id,
                    property_name,
                    lane.value,
                    scenario_branch_id or "",
                ),
            ).fetchone()
        return _property_from_row(row) if row else None

    def upsert_property(self, value: TwinPropertyState) -> None:
        """Upsert the current state for one lane/branch key."""
        with self._connect(value.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_twin_property_state
                    (tenant_id, twin_id, property_name, lane, scenario_branch_key, value, unit,
                     observation_id, observed_at, projected_at, source_system, source_quality,
                     confidence, freshness)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, twin_id, property_name, lane, scenario_branch_key)
                do update set value = excluded.value, unit = excluded.unit,
                    observation_id = excluded.observation_id, observed_at = excluded.observed_at,
                    projected_at = excluded.projected_at, source_system = excluded.source_system,
                    source_quality = excluded.source_quality, confidence = excluded.confidence,
                    freshness = excluded.freshness
                """,
                (
                    value.tenant_id,
                    value.twin_id,
                    value.property_name,
                    value.lane.value,
                    value.scenario_branch_id or "",
                    jsonb(value.value),
                    value.unit,
                    value.observation_id,
                    value.observed_at,
                    value.projected_at,
                    value.source_system,
                    value.source_quality,
                    value.confidence,
                    value.freshness.value,
                ),
            )
            conn.commit()

    def list_properties(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> list[TwinPropertyState]:
        """Return projected properties, optionally narrowed by endpoint store."""
        query = """
            select p.tenant_id, p.twin_id, p.property_name, p.lane, p.value, p.unit,
                   p.observation_id, p.observed_at, p.projected_at, p.source_system,
                   p.source_quality, p.confidence, p.freshness, p.scenario_branch_key
            from shelfwise_twin_property_state p
        """
        params: list[Any] = [tenant_id]
        if store_id is not None:
            query += (
                " join shelfwise_twin_entities e on e.tenant_id = p.tenant_id "
                "and e.twin_id = p.twin_id"
            )
        query += " where p.tenant_id = %s"
        if store_id is not None:
            query += " and e.store_id = %s"
            params.append(store_id)
        query += " order by p.twin_id, p.property_name"
        with self._connect(tenant_id) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_property_from_row(row) for row in rows]

    def clear(self) -> None:
        """Clear rows visible to the active tenant for disposable test databases."""
        with self._connect(None) as conn:
            for table in (
                "shelfwise_twin_property_state",
                "shelfwise_twin_observations",
                "shelfwise_twin_relationships",
                "shelfwise_twin_entities",
            ):
                conn.execute(f"delete from {table}")
            conn.commit()

    def _ensure_schema(self) -> None:
        """Create the additive twin tables before first use in local Postgres."""
        with self._connect(None) as conn:
            for statement in TWIN_SCHEMA_SQL:
                conn.execute(statement)
            apply_tenant_rls(conn, TWIN_TABLES)
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


TWIN_TABLES = (
    "shelfwise_twin_entities",
    "shelfwise_twin_relationships",
    "shelfwise_twin_observations",
    "shelfwise_twin_property_state",
)

TWIN_SCHEMA_SQL = (
    """
    create table if not exists shelfwise_twin_entities (
        tenant_id text not null, twin_id text not null, store_id text not null,
        entity_type text not null, model_version text not null, display_name text not null,
        attributes jsonb not null default '{}', created_at timestamptz not null,
        retired_at timestamptz, primary key (tenant_id, twin_id)
    )
    """,
    """
    create table if not exists shelfwise_twin_relationships (
        tenant_id text not null, relationship_id text not null, source_twin_id text not null,
        relationship_type text not null, target_twin_id text not null,
        attributes jsonb not null default '{}', valid_from timestamptz not null,
        valid_to timestamptz, primary key (tenant_id, relationship_id),
        foreign key (tenant_id, source_twin_id)
            references shelfwise_twin_entities (tenant_id, twin_id),
        foreign key (tenant_id, target_twin_id)
            references shelfwise_twin_entities (tenant_id, twin_id)
    )
    """,
    """
    create table if not exists shelfwise_twin_observations (
        tenant_id text not null, observation_id text not null, store_id text not null,
        twin_id text not null, property_name text not null, lane text not null
          check (lane in ('reported', 'estimated', 'desired', 'predicted')),
        value jsonb not null, unit text, observed_at timestamptz not null,
        ingested_at timestamptz not null, source_system text not null,
        source_object_id text not null, source_sequence text,
        source_quality double precision not null check (source_quality between 0 and 1),
        schema_version text not null, correlation_id text not null, causation_id text,
        scenario_branch_id text, payload_hash text not null,
        primary key (tenant_id, observation_id),
        foreign key (tenant_id, twin_id)
            references shelfwise_twin_entities (tenant_id, twin_id),
        check (lane <> 'predicted' or scenario_branch_id is not null)
    )
    """,
    """
    create unique index if not exists ux_shelfwise_twin_observation_source
    on shelfwise_twin_observations
      (tenant_id, source_system, source_object_id, property_name, lane, payload_hash)
    """,
    """
    create table if not exists shelfwise_twin_property_state (
        tenant_id text not null, twin_id text not null, property_name text not null,
        lane text not null check (lane in ('reported', 'estimated', 'desired', 'predicted')),
        scenario_branch_key text not null default '', value jsonb not null, unit text,
        observation_id text not null, observed_at timestamptz not null,
        projected_at timestamptz not null, source_system text not null,
        source_quality double precision not null check (source_quality between 0 and 1),
        confidence double precision not null check (confidence between 0 and 1),
        freshness text not null, primary key
          (tenant_id, twin_id, property_name, lane, scenario_branch_key)
    )
    """,
    """
    create index if not exists idx_shelfwise_twin_observations_entity_time
    on shelfwise_twin_observations (tenant_id, twin_id, observed_at desc)
    """,
)


def create_twin_store() -> InMemoryTwinStore | PostgresTwinStore:
    """Create the twin store using the same backend switch as existing state stores."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryTwinStore()
    if backend == "postgres":
        return PostgresTwinStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _property_key(
    tenant_id: str,
    twin_id: str,
    property_name: str,
    lane: StateLane,
    scenario_branch_id: str | None,
) -> tuple[str, str, str, str, str]:
    """Build the tenant/entity/property/lane/branch state key."""
    return (tenant_id, twin_id, property_name, lane.value, scenario_branch_id or "")


def _entity_from_row(row: dict[str, Any]) -> TwinEntity:
    """Convert a Postgres row into the validated entity contract."""
    return TwinEntity(
        tenant_id=str(row["tenant_id"]),
        twin_id=str(row["twin_id"]),
        store_id=str(row["store_id"]),
        entity_type=str(row["entity_type"]),
        model_version=str(row["model_version"]),
        display_name=str(row["display_name"]),
        attributes=row["attributes"] or {},
        created_at=_as_datetime(row["created_at"]),
        retired_at=_as_optional_datetime(row.get("retired_at")),
    )


def _relationship_from_row(row: dict[str, Any]) -> TwinRelationship:
    """Convert a Postgres row into the validated relationship contract."""
    return TwinRelationship(
        tenant_id=str(row["tenant_id"]),
        relationship_id=str(row["relationship_id"]),
        source_twin_id=str(row["source_twin_id"]),
        relationship_type=str(row["relationship_type"]),
        target_twin_id=str(row["target_twin_id"]),
        attributes=row["attributes"] or {},
        valid_from=_as_datetime(row["valid_from"]),
        valid_to=_as_optional_datetime(row.get("valid_to")),
    )


def _observation_from_row(row: dict[str, Any]) -> TwinObservation:
    """Convert a Postgres row into the immutable observation contract."""
    return TwinObservation(
        observation_id=str(row["observation_id"]),
        tenant_id=str(row["tenant_id"]),
        store_id=str(row["store_id"]),
        twin_id=str(row["twin_id"]),
        property_name=str(row["property_name"]),
        lane=StateLane(str(row["lane"])),
        value=row["value"],
        unit=row.get("unit"),
        observed_at=_as_datetime(row["observed_at"]),
        ingested_at=_as_datetime(row["ingested_at"]),
        source_system=str(row["source_system"]),
        source_object_id=str(row["source_object_id"]),
        source_sequence=row.get("source_sequence"),
        source_quality=float(row["source_quality"]),
        schema_version=str(row["schema_version"]),
        correlation_id=str(row["correlation_id"]),
        causation_id=row.get("causation_id"),
        scenario_branch_id=row.get("scenario_branch_id"),
        payload_hash=str(row["payload_hash"]),
    )


def _property_from_row(row: dict[str, Any]) -> TwinPropertyState:
    """Convert a Postgres row into the current-state contract."""
    return TwinPropertyState(
        tenant_id=str(row["tenant_id"]),
        twin_id=str(row["twin_id"]),
        property_name=str(row["property_name"]),
        lane=StateLane(str(row["lane"])),
        value=row["value"],
        unit=row.get("unit"),
        observation_id=str(row["observation_id"]),
        observed_at=_as_datetime(row["observed_at"]),
        projected_at=_as_datetime(row["projected_at"]),
        source_system=str(row["source_system"]),
        source_quality=float(row["source_quality"]),
        confidence=float(row["confidence"]),
        freshness=FreshnessState(str(row["freshness"])),
        scenario_branch_id=row.get("scenario_branch_key") or None,
    )


def _as_datetime(value: Any) -> datetime:
    """Normalize driver-returned timestamps to timezone-aware datetimes."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_optional_datetime(value: Any) -> datetime | None:
    """Normalize nullable driver timestamps."""
    return _as_datetime(value) if value is not None else None
