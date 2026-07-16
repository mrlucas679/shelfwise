"""Bounded sensor calibration records used by fidelity and fail-closed ingestion."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_storage import auto_schema_enabled, connect
from shelfwise_storage.rls import apply_tenant_rls


class CalibrationRequest(BaseModel):
    """API payload for comparing one sensor reading with a trusted reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: str = Field(min_length=8, max_length=160)
    property_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,127}$")
    reference_value: float
    observed_value: float
    tolerance: float = Field(gt=0)


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """One auditable device/property calibration result."""

    tenant_id: str
    store_id: str
    device_id: str
    property_name: str
    reference_value: float
    observed_value: float
    tolerance: float
    calibrated_at: datetime
    calibration_id: str

    @property
    def score(self) -> float:
        """Return 1 when within tolerance and degrade linearly outside it."""
        error = abs(self.reference_value - self.observed_value)
        return max(0.0, min(1.0, 1.0 - error / max(self.tolerance, 1e-9)))


def _calibration_id(tenant_id: str, store_id: str, device_id: str, property_name: str) -> str:
    return f"cal_{tenant_id}_{store_id}_{device_id}_{property_name}"[:200]


class CalibrationRegistry(Protocol):
    """Storage contract shared by the disposable local and durable Postgres runtimes."""

    def record(
        self,
        *,
        tenant_id: str,
        store_id: str,
        device_id: str,
        property_name: str,
        reference_value: float,
        observed_value: float,
        tolerance: float,
    ) -> CalibrationRecord: ...

    def score(self, tenant_id: str, store_id: str) -> float: ...

    def list(self, tenant_id: str, store_id: str) -> list[CalibrationRecord]: ...

    def clear(self) -> None: ...


class InMemoryCalibrationRegistry:
    """Process-local calibration registry with tenant/store isolation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str, str, str], CalibrationRecord] = {}

    def record(
        self, *, tenant_id: str, store_id: str, device_id: str, property_name: str,
        reference_value: float, observed_value: float, tolerance: float,
    ) -> CalibrationRecord:
        """Store one bounded reading comparison and return its immutable receipt."""
        if tolerance <= 0:
            raise ValueError("calibration tolerance must be positive")
        record = CalibrationRecord(
            tenant_id, store_id, device_id, property_name, float(reference_value),
            float(observed_value), float(tolerance), datetime.now(UTC),
            _calibration_id(tenant_id, store_id, device_id, property_name),
        )
        with self._lock:
            self._records[(tenant_id, store_id, device_id, property_name)] = record
        return record

    def score(self, tenant_id: str, store_id: str) -> float:
        """Return the mean score for calibrated devices, or zero when none exist."""
        with self._lock:
            rows = [
                r for r in self._records.values()
                if r.tenant_id == tenant_id and r.store_id == store_id
            ]
        return sum(row.score for row in rows) / len(rows) if rows else 0.0

    def list(self, tenant_id: str, store_id: str) -> list[CalibrationRecord]:
        """Return calibration receipts only for the requested tenant and store."""
        with self._lock:
            return [
                r for r in self._records.values()
                if r.tenant_id == tenant_id and r.store_id == store_id
            ]

    def clear(self) -> None:
        """Clear local calibration state between tests or disposable demo runs."""
        with self._lock:
            self._records.clear()


class PostgresCalibrationRegistry:
    """Durable calibration registry protected by tenant RLS.

    Calibration feeds `TwinService.fidelity()`'s `calibration_score`/`calibration_complete`
    fields; an in-memory-only registry would silently reset that score to zero on every
    process restart, which is indistinguishable from "never calibrated" - a real recovery
    gap, not a cosmetic one.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresCalibrationRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(
        self, *, tenant_id: str, store_id: str, device_id: str, property_name: str,
        reference_value: float, observed_value: float, tolerance: float,
    ) -> CalibrationRecord:
        if tolerance <= 0:
            raise ValueError("calibration tolerance must be positive")
        record = CalibrationRecord(
            tenant_id, store_id, device_id, property_name, float(reference_value),
            float(observed_value), float(tolerance), datetime.now(UTC),
            _calibration_id(tenant_id, store_id, device_id, property_name),
        )
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_twin_calibrations
                    (tenant_id, store_id, device_id, property_name, reference_value,
                     observed_value, tolerance, calibrated_at, calibration_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, store_id, device_id, property_name) do update set
                    reference_value = excluded.reference_value,
                    observed_value = excluded.observed_value,
                    tolerance = excluded.tolerance,
                    calibrated_at = excluded.calibrated_at,
                    calibration_id = excluded.calibration_id
                """,
                (
                    record.tenant_id, record.store_id, record.device_id, record.property_name,
                    record.reference_value, record.observed_value, record.tolerance,
                    record.calibrated_at, record.calibration_id,
                ),
            )
            conn.commit()
        return record

    def score(self, tenant_id: str, store_id: str) -> float:
        rows = self.list(tenant_id, store_id)
        return sum(row.score for row in rows) / len(rows) if rows else 0.0

    def list(self, tenant_id: str, store_id: str) -> list[CalibrationRecord]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                """
                select tenant_id, store_id, device_id, property_name, reference_value,
                       observed_value, tolerance, calibrated_at, calibration_id
                from shelfwise_twin_calibrations
                where tenant_id = %s and store_id = %s
                order by device_id, property_name
                """,
                (tenant_id, store_id),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_twin_calibrations")
            conn.commit()

    def _ensure_schema(self) -> None:
        """Create the additive calibration table before first use in local Postgres."""
        with self._connect(None) as conn:
            for statement in CALIBRATION_SCHEMA_SQL:
                conn.execute(statement)
            apply_tenant_rls(conn, CALIBRATION_TABLES)
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def _record_from_row(row: Any) -> CalibrationRecord:
    return CalibrationRecord(
        tenant_id=row["tenant_id"],
        store_id=row["store_id"],
        device_id=row["device_id"],
        property_name=row["property_name"],
        reference_value=row["reference_value"],
        observed_value=row["observed_value"],
        tolerance=row["tolerance"],
        calibrated_at=row["calibrated_at"],
        calibration_id=row["calibration_id"],
    )


CALIBRATION_TABLES = ("shelfwise_twin_calibrations",)

CALIBRATION_SCHEMA_SQL = (
    """
    create table if not exists shelfwise_twin_calibrations (
        tenant_id text not null, store_id text not null, device_id text not null,
        property_name text not null, reference_value double precision not null,
        observed_value double precision not null,
        tolerance double precision not null check (tolerance > 0),
        calibrated_at timestamptz not null, calibration_id text not null,
        primary key (tenant_id, store_id, device_id, property_name)
    )
    """,
)


def create_calibration_registry() -> InMemoryCalibrationRegistry | PostgresCalibrationRegistry:
    """Create the calibration registry using the same backend switch as other twin stores."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryCalibrationRegistry()
    if backend == "postgres":
        return PostgresCalibrationRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")
