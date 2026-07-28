"""Owner-facing, server-derived setup progress for the guided onboarding flow."""

from __future__ import annotations

from fastapi import APIRouter

from shelfwise_edge import edge_device_registry

from .deps import OWNER_AUTH_DEP
from .state import (
    account_store,
    connector_credential_store,
    inbound_record_store,
    tenant_profile_store,
    twin_service,
)
from .tenant import TenantContext

router = APIRouter()


@router.get("/onboarding/status")
def onboarding_status(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """Build resumable onboarding progress from authoritative tenant-scoped stores.

    No browser-only completion flags are accepted. A tenant is operationally ready after
    saving a company profile, explicitly onboarding a store, and configuring at least
    one connector or importing at least one valid record. Devices and extra workforce
    accounts are recommended but optional because not every shop owns compatible hardware
    or needs delegated access on day one.
    """
    profile = tenant_profile_store.get(ctx.tenant_id)
    manifests = twin_service.onboarding_manifests.list(ctx.tenant_id)
    configured_systems = sorted(
        system.value
        for system in connector_credential_store.list_configured_systems(
            tenant_id=ctx.tenant_id
        )
    )
    imported_records = inbound_record_store.list(tenant_id=ctx.tenant_id, limit=1)
    devices = edge_device_registry.list_devices(ctx.tenant_id)
    active_devices = [device for device in devices if bool(device.get("active"))]
    accounts = account_store.list(ctx.tenant_id)
    active_accounts = [account for account in accounts if bool(account.get("active"))]

    profile_configured = profile is not None
    store_configured = bool(manifests)
    data_source_configured = bool(configured_systems or imported_records)
    completed_required = sum(
        (profile_configured, store_configured, data_source_configured)
    )
    next_required_step = (
        "company"
        if not profile_configured
        else "store"
        if not store_configured
        else "data"
        if not data_source_configured
        else "review"
    )

    return {
        "tenant_id": ctx.tenant_id,
        "ready_for_operations": completed_required == 3,
        "required_steps": {
            "completed": completed_required,
            "total": 3,
            "next": next_required_step,
        },
        "company": {
            "configured": profile_configured,
            "name": str((profile or {}).get("name") or ""),
        },
        "stores": [
            {
                "store_id": manifest.store_id,
                "display_name": manifest.display_name,
                "timezone": manifest.timezone,
                "entity_count": len(manifest.entities) + 1,
            }
            for manifest in manifests
        ],
        "data": {
            "configured": data_source_configured,
            "connector_systems": configured_systems,
            "has_imported_records": bool(imported_records),
        },
        "devices": {
            "active": len(active_devices),
            "total": len(devices),
        },
        "accounts": {
            "active": len(active_accounts),
            "total": len(accounts),
        },
    }
