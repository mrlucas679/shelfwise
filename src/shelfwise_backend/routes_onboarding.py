"""Owner-facing, server-derived setup progress for the guided onboarding flow."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shelfwise_edge import edge_device_registry

from .deps import OWNER_AUTH_DEP, WRITE_LIMIT_DEP, write_path_guard
from .product_policies import (
    list_product_policy_templates,
    registered_product_policy,
)
from .state import (
    account_store,
    connector_credential_store,
    inbound_record_store,
    policy_confirmation_store,
    tenant_profile_store,
    twin_service,
)
from .tenant import TenantContext

router = APIRouter()


class PolicyConfirmationBody(BaseModel):
    """Owner-selected categories whose current templates were reviewed."""

    categories: list[str] = Field(min_length=1, max_length=16)


@router.get("/onboarding/policies")
def onboarding_policies(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """List current templates and this tenant's durable confirmations."""
    templates = list_product_policy_templates()
    confirmations = policy_confirmation_store.list(ctx.tenant_id)
    current_ids = {
        str(template["category"]): str(template["policy_id"]) for template in templates
    }
    current_confirmations = [
        confirmation
        for confirmation in confirmations
        if current_ids.get(confirmation["category"]) == confirmation["policy_id"]
    ]
    return {
        "templates": templates,
        "confirmations": confirmations,
        "current_confirmation_count": len(current_confirmations),
    }


@router.post(
    "/onboarding/policies/confirm",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def confirm_onboarding_policies(
    body: PolicyConfirmationBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Record owner review against the exact templates used by the cascade."""
    normalized_categories = list(
        dict.fromkeys(category.strip().lower() for category in body.categories)
    )
    if any(not category for category in normalized_categories):
        raise HTTPException(status_code=422, detail="Policy categories cannot be empty")
    confirmations = []
    for category in normalized_categories:
        try:
            policy = registered_product_policy(category)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        confirmations.append(
            policy_confirmation_store.confirm(
                tenant_id=ctx.tenant_id,
                category=category,
                policy_id=policy.policy_id,
                confirmed_by=ctx.user_id,
            )
        )
    return {
        "confirmed": confirmations,
        "current_confirmation_count": len(
            _current_policy_confirmations(ctx.tenant_id)
        ),
    }


@router.get("/onboarding/status")
def onboarding_status(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """Build resumable onboarding progress from authoritative tenant-scoped stores.

    No browser-only completion flags are accepted. A tenant is operationally ready after
    saving a company profile, explicitly onboarding a store, configuring at least one
    connector or importing at least one valid record, and confirming at least one current
    product-policy template. Devices and extra workforce accounts remain optional.
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
    current_policy_confirmations = _current_policy_confirmations(ctx.tenant_id)

    profile_configured = profile is not None
    store_configured = bool(manifests)
    data_source_configured = bool(configured_systems or imported_records)
    policies_confirmed = bool(current_policy_confirmations)
    completed_required = sum(
        (
            profile_configured,
            store_configured,
            data_source_configured,
            policies_confirmed,
        )
    )
    next_required_step = (
        "company"
        if not profile_configured
        else "store"
        if not store_configured
        else "data"
        if not data_source_configured
        else "policies"
        if not policies_confirmed
        else "review"
    )

    return {
        "tenant_id": ctx.tenant_id,
        "ready_for_operations": completed_required == 4,
        "required_steps": {
            "completed": completed_required,
            "total": 4,
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
        "policies": {
            "configured": policies_confirmed,
            "confirmed_categories": [
                confirmation["category"] for confirmation in current_policy_confirmations
            ],
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


def _current_policy_confirmations(tenant_id: str) -> list[dict[str, str]]:
    """Ignore confirmations for superseded policy template versions."""
    current_ids = {
        str(template["category"]): str(template["policy_id"])
        for template in list_product_policy_templates()
    }
    return [
        confirmation
        for confirmation in policy_confirmation_store.list(tenant_id)
        if current_ids.get(confirmation["category"]) == confirmation["policy_id"]
    ]
