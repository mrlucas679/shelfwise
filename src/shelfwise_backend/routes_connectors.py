"""Read-only connector discovery and inbound-record listing routes.

Third API router split out of `app.py`'s single-file route list, following the same
pattern `routes_twin.py` and `routes_catalog.py` established. Depends only on the
shared `tenant_profile_store`/`inbound_record_store` singletons (`state.py`) and stable
`shelfwise_connectors` package functions - no cross-talk with chat, decisions, or the
cascade pipeline. The connector *write* routes (`/connectors/{system}/intake`,
`/connectors/poll/status`) stay in `app.py`: they depend on `_process_inbound_record`,
an app.py-local helper shared with the CSV-intake routes, and extracting only the
read-only half here avoids relocating that coupling instead of resolving it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from shelfwise_connectors import connector_status_for_policy, list_connector_capabilities
from shelfwise_storage import default_tenant_profile

from .deps import CURRENT_TENANT_DEP
from .state import inbound_record_store, tenant_profile_store
from .tenant import TenantContext

router = APIRouter()


@router.get("/connectors/systems")
def list_connector_systems() -> dict[str, object]:
    return {"systems": [item.to_dict() for item in list_connector_capabilities()]}


@router.get("/connectors/me")
def list_tenant_connectors(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    profile = tenant_profile_store.get(ctx.tenant_id) or default_tenant_profile(ctx.tenant_id)
    policy = (
        profile.get("connector_policy") if isinstance(profile.get("connector_policy"), dict) else {}
    )
    return {
        "tenant_id": ctx.tenant_id,
        "connector_policy": policy,
        "systems": connector_status_for_policy(policy),
    }


@router.get("/connectors/inbound-records")
def list_inbound_records(
    limit: int = 200,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    try:
        records = inbound_record_store.list(tenant_id=ctx.tenant_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"tenant_id": ctx.tenant_id, "records": records}
