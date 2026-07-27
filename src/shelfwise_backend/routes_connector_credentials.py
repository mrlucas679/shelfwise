"""Owner-only CRUD for a tenant's own encrypted connector credentials.

Kept out of `routes_connectors.py` deliberately - that router is documented read-only,
and mixing a credential-write surface into it would contradict its own docstring. This is
the real multi-tenant credential-storage capability HANDOFF.md's 2026-07-23
"Connector credentials" entry describes: `POST` stores fields encrypted (Fernet, see
`shelfwise_connectors.credentials`) via `connector_credential_store`
(`state.py`), owner-role only, never returned in plaintext by any route in this file -
`GET` returns only which systems are configured, not their values.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shelfwise_connectors import ConnectorCredentialError, CredentialEncryptionError, SourceSystem

from .deps import OWNER_AUTH_DEP, WRITE_LIMIT_DEP, write_path_guard
from .state import connector_credential_store
from .tenant import TenantContext

router = APIRouter()


class ConnectorCredentialBody(BaseModel):
    fields: dict[str, str] = Field(min_length=1)


@router.get("/connectors/{system}/credentials")
def list_connector_credential_status(
    system: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Report only whether this tenant has stored credentials for a system, never values."""
    try:
        resolved_system = SourceSystem(system)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown connector system") from exc
    configured = resolved_system in connector_credential_store.list_configured_systems(
        tenant_id=ctx.tenant_id
    )
    return {"system": resolved_system.value, "configured": configured}


@router.post(
    "/connectors/{system}/credentials",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_connector_credentials(
    system: str,
    body: ConnectorCredentialBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    try:
        resolved_system = SourceSystem(system)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown connector system") from exc
    try:
        connector_credential_store.upsert(
            tenant_id=ctx.tenant_id, system=resolved_system, fields=body.fields
        )
    except (ConnectorCredentialError, CredentialEncryptionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"system": resolved_system.value, "configured": True}


@router.post(
    "/connectors/{system}/credentials/delete",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def delete_connector_credentials(
    system: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    try:
        resolved_system = SourceSystem(system)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown connector system") from exc
    connector_credential_store.delete(tenant_id=ctx.tenant_id, system=resolved_system)
    return {"system": resolved_system.value, "configured": False}
