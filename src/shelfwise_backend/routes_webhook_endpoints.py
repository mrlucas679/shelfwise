"""Self-serve provisioning and signature-authenticated delivery for retailer webhooks.

The missing half of "a shop owner can connect their own systems". Poll-based ERPs already
had a self-serve credential form; the four signature-authenticated systems (Shopify, Square,
Lightspeed, Yoco) did not, because `/connectors/{system}/intake` is gated by the shared
ingest API key. That key belongs to the operator, so connecting a POS meant a developer had
to configure it - the exact step this router removes.

An owner provisions an endpoint, receives a URL and signing secret once, and pastes both
into their retailer's webhook settings. Deliveries then authenticate on the HMAC signature
alone against that tenant's own secret, using the same `verify_signature` implementation the
connector receivers already use.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from shelfwise_connectors import (
    MAX_WEBHOOK_BYTES,
    WEBHOOK_SYSTEMS,
    WebhookEndpoint,
    WebhookEndpointError,
    WebhookSystemNotSupported,
    map_for,
    new_endpoint_credentials,
    resolve_webhook_system,
    verify_signature,
)

from .deps import OWNER_AUTH_DEP, WRITE_LIMIT_DEP, write_path_guard
from .state import webhook_endpoint_registry
from .tenant import TenantContext

_LOG = logging.getLogger("shelfwise.webhook_endpoints")

router = APIRouter()

# Injected by app.py so this router does not import from it (the same seam
# connector_poll_service uses to reach the shared ingestion pipeline).
ProcessRecord = Any
_process_record: ProcessRecord | None = None


def bind_record_processor(processor: ProcessRecord) -> None:
    """Wire the shared inbound-record pipeline without importing app.py."""
    global _process_record
    _process_record = processor


@router.get("/connectors/webhook-endpoints")
def list_webhook_endpoints(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """List this tenant's provisioned endpoints, never returning a signing secret."""
    return {
        "tenant_id": ctx.tenant_id,
        "supported_systems": sorted(system.value for system in WEBHOOK_SYSTEMS),
        "endpoints": webhook_endpoint_registry.list_endpoints(ctx.tenant_id),
    }


@router.post(
    "/connectors/{system}/webhook-endpoint",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def provision_webhook_endpoint(
    system: str,
    request: Request,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Provision one endpoint and return its signing secret exactly once.

    The secret is not recoverable afterwards - `list_webhook_endpoints` never returns it and
    the database holds only ciphertext - so an owner who loses it provisions a new endpoint
    and revokes the old one rather than reading the original back out.
    """
    try:
        resolved = resolve_webhook_system(system)
    except WebhookSystemNotSupported as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    endpoint_id, signing_secret = new_endpoint_credentials()
    try:
        webhook_endpoint_registry.register(
            WebhookEndpoint(
                endpoint_id=endpoint_id,
                tenant_id=ctx.tenant_id,
                system=resolved,
                signing_secret=signing_secret,
            )
        )
    except WebhookEndpointError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "endpoint_id": endpoint_id,
        "system": resolved.value,
        "signing_secret": signing_secret,
        "delivery_url": str(request.url_for("receive_tenant_webhook", endpoint_id=endpoint_id)),
        "signature_header": "x-shelfwise-signature",
    }


@router.post(
    "/connectors/webhook-endpoints/{endpoint_id}/revoke",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def revoke_webhook_endpoint(
    endpoint_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Disable one of this tenant's endpoints; another tenant's id can never be revoked."""
    revoked = webhook_endpoint_registry.revoke(endpoint_id, tenant_id=ctx.tenant_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Unknown or already revoked endpoint")
    return {"endpoint_id": endpoint_id, "active": False}


@router.post("/connectors/webhook/{endpoint_id}", name="receive_tenant_webhook")
async def receive_tenant_webhook(
    endpoint_id: str,
    request: Request,
    x_shelfwise_signature: str = Header(min_length=16, max_length=200),
) -> dict[str, object]:
    """Accept a retailer delivery authenticated only by this tenant's signing secret.

    Deliberately not behind `write_path_guard`/the ingest role: requiring the operator's
    shared API key here is precisely what prevented a store owner from connecting their own
    POS. The signature over the raw body is the authentication, and the endpoint id resolves
    which tenant the delivery belongs to.
    """
    body = await request.body()
    if len(body) == 0 or len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Invalid webhook payload size")
    try:
        endpoint = webhook_endpoint_registry.get_active(endpoint_id)
    except WebhookEndpointError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # One uniform 401 for unknown, revoked, and badly-signed endpoints so this route cannot
    # be used to probe which endpoint ids exist.
    if endpoint is None or not verify_signature(
        endpoint.signing_secret, body, x_shelfwise_signature
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook body must be a JSON object")

    try:
        records = map_for(endpoint.system, payload, tenant_id=endpoint.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {endpoint.system.value} webhook payload",
        ) from exc

    if _process_record is None:  # pragma: no cover - wiring guard
        raise HTTPException(status_code=503, detail="Ingestion pipeline is unavailable")
    outcomes = [_process_record(record) for record in records]
    return {
        "status": outcomes[0]["status"],
        "system": endpoint.system.value,
        "records": outcomes,
    }
