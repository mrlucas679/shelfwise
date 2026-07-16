"""Digital-twin store/entity/observation/fidelity routes.

The first API router split out of `app.py`'s single-file route list. Depends only on the
shared `twin_service` singleton (`state.py`) and the tenant/write-path dependencies
(`deps.py`) - no cross-talk with chat, decisions, or the cascade pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from shelfwise_contracts import Event
from shelfwise_edge import EdgeObservationBatch, edge_device_registry, verify_signed_body
from shelfwise_runtime.provenance import DataDomain
from shelfwise_twin import (
    CalibrationRequest,
    ScenarioRequest,
    TwinObservation,
    TwinOnboardingManifest,
)

from .deps import CURRENT_TENANT_DEP, INGEST_AUTH_DEP, WRITE_LIMIT_DEP, write_path_guard
from .state import event_store, scenario_engine, twin_service
from .tenant import TenantContext

router = APIRouter()


@router.post(
    "/twin/stores/{store_id}/calibration",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def calibrate_twin_device(
    store_id: str,
    body: CalibrationRequest,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Record a bounded device calibration result for the store fidelity rail."""
    device = edge_device_registry.get_active(body.device_id)
    if device is None or device.tenant_id != ctx.tenant_id or device.store_id != store_id:
        raise HTTPException(status_code=403, detail="Calibration device scope mismatch")
    try:
        record = twin_service.calibrations.record(
            tenant_id=ctx.tenant_id, store_id=store_id, device_id=body.device_id,
            property_name=body.property_name, reference_value=body.reference_value,
            observed_value=body.observed_value, tolerance=body.tolerance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"calibration": {"calibration_id": record.calibration_id, "score": record.score}}


@router.get("/twin/stores/{store_id}/devices")
def list_twin_devices(
    store_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Expose device health metadata without exposing signing secrets."""
    return {"devices": edge_device_registry.list_devices(ctx.tenant_id, store_id)}


@router.post(
    "/twin/stores/{store_id}/scenarios",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def create_twin_scenario(
    store_id: str,
    body: ScenarioRequest,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Run an isolated what-if branch without mutating reported store state."""
    try:
        return scenario_engine.create(ctx.tenant_id, store_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/twin/stores/{store_id}/scenarios/{branch_id}")
def compare_twin_scenario(
    store_id: str,
    branch_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Compare one predicted branch with the current observed projection."""
    try:
        return scenario_engine.compare(ctx.tenant_id, store_id, branch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/twin/onboarding",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def onboard_twin(
    body: TwinOnboardingManifest,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Bind the twin to one named shop and seed its initial topology."""
    if body.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="Onboarding tenant does not match token")
    try:
        return twin_service.onboard(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/twin/stores/{store_id}")
def get_store_twin(
    store_id: str,
    limit: int = 200,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return the tenant-scoped exact-store topology and projected state."""
    try:
        return twin_service.get_store(ctx.tenant_id, store_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/twin/stores/{store_id}/snapshot")
def get_twin_snapshot(
    store_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return the deterministic snapshot identity used by tools and conversation context."""
    return twin_service.snapshot(ctx.tenant_id, store_id)


@router.post(
    "/twin/stores/{store_id}/bootstrap",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def bootstrap_twin_from_events(
    store_id: str,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Replay existing canonical events without executing their cascades again."""
    try:
        rows = event_store.list(
            limit=500,
            tenant_id=ctx.tenant_id,
            data_domain=DataDomain.OPERATIONAL_TWIN,
        )
        events = _parse_events(rows)
        return twin_service.bootstrap_events(
            events,
            tenant_id=ctx.tenant_id,
            store_id=store_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/twin/entities/{twin_id:path}")
def get_twin_entity(
    twin_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return one entity with its current properties, history, and relationships."""
    result = twin_service.get_entity(ctx.tenant_id, twin_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Twin entity not found")
    return result


@router.get("/twin/observations")
def list_twin_observations(
    store_id: str | None = None,
    limit: int = 200,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return bounded immutable observations without exposing raw media."""
    try:
        rows = twin_service.store.list_observations(
            ctx.tenant_id,
            store_id=store_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"observations": [row.model_dump(mode="json") for row in rows]}


@router.post(
    "/twin/observations",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def ingest_twin_observation(
    body: TwinObservation,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    """Accept one tenant-bound derived observation and project it idempotently."""
    if body.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="Observation tenant does not match token")
    try:
        result = twin_service.accept(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"result": result.to_dict()}


@router.get("/twin/fidelity")
def get_twin_fidelity(
    store_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Expose dimensioned twin fidelity and hard guards for one store."""
    devices = edge_device_registry.list_devices(ctx.tenant_id, store_id)
    expected_device_ids = frozenset(str(device["device_id"]) for device in devices)
    return twin_service.fidelity(ctx.tenant_id, store_id, expected_device_ids=expected_device_ids)


@router.post(
    "/twin/edge/observations",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
async def ingest_edge_observations(
    request: Request,
    x_shelfwise_device: str = Header(min_length=8, max_length=160),
    x_shelfwise_signature: str = Header(pattern=r"^sha256=[a-f0-9]{64}$"),
) -> dict[str, object]:
    """Authenticate a device and ingest derived observations without retaining raw media."""
    # Content-Length is only a client-declared hint - a caller can send a small header with
    # a large body. The global body-size middleware in app.py bounds the read itself, but
    # this route's tighter 512 KB edge-payload cap must be checked against the actual bytes
    # received, not the header a sender is free to lie about.
    body = await request.body()
    if len(body) == 0 or len(body) > 512_000:
        raise HTTPException(status_code=413, detail="Invalid edge payload size")
    device = edge_device_registry.get_active(x_shelfwise_device)
    if device is None or not verify_signed_body(body, x_shelfwise_signature, device.hmac_secret):
        raise HTTPException(status_code=401, detail="Invalid edge device signature")
    try:
        batch = EdgeObservationBatch.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid derived observation batch") from exc
    if (
        batch.device_id != device.device_id
        or batch.tenant_id != device.tenant_id
        or batch.store_id != device.store_id
        or any(
            item.tenant_id != device.tenant_id or item.store_id != device.store_id
            for item in batch.observations
        )
    ):
        raise HTTPException(status_code=403, detail="Edge batch scope mismatch")
    if not edge_device_registry.record_batch(batch.tenant_id, batch.batch_id):
        return {"status": "duplicate", "batch_id": batch.batch_id, "accepted": 0}
    receipts = [twin_service.accept(item) for item in batch.observations]
    return {
        "status": "accepted",
        "batch_id": batch.batch_id,
        "accepted": sum(result.status == "projected" for result in receipts),
        "duplicates": sum(result.status == "duplicate" for result in receipts),
    }


def _parse_events(rows: list[dict[str, object]]) -> list[Event]:
    """Parse stored canonical rows while ignoring one malformed historical row safely."""
    events: list[Event] = []
    for row in rows:
        try:
            events.append(Event.parse_wire(row))
        except ValueError:
            continue
    return events
