from __future__ import annotations

from collections.abc import Callable

from ...canonical import SourceSystem
from ...provenance import InboundRecord
from .dynamics import map_dynamics_inventory
from .lightspeed import map_lightspeed_sale
from .odoo import map_odoo_product
from .sap import map_sap_inventory
from .shopify import map_shopify_order
from .square import map_square_inventory
from .syspro import map_syspro_inventory
from .yoco import map_yoco_checkout

# Webhook mappers may fan a single payload out into multiple records (e.g. one sales
# line per line item); poll mappers process one source row per call.
WebhookMapper = Callable[[dict, str], list[InboundRecord]]
PollMapper = Callable[[dict, str], InboundRecord]

WEBHOOK_MAPPERS: dict[SourceSystem, WebhookMapper] = {
    SourceSystem.LIGHTSPEED: map_lightspeed_sale,
    SourceSystem.SHOPIFY: map_shopify_order,
    SourceSystem.SQUARE: map_square_inventory,
    SourceSystem.YOCO: map_yoco_checkout,
}

POLL_MAPPERS: dict[SourceSystem, PollMapper] = {
    # Manual intake must name its warehouse just as scheduled polling does.  A Business
    # Central item resource has no warehouse dimension, so inventing a default would
    # create an unsafe stock fact.
    SourceSystem.DYNAMICS: lambda payload, tenant_id: map_dynamics_inventory(
        payload,
        tenant_id=tenant_id,
        location_id=str(payload.get("location_id") or "").strip(),
    ),
    SourceSystem.ODOO: map_odoo_product,
    SourceSystem.SAP: map_sap_inventory,
    SourceSystem.SYSPRO: map_syspro_inventory,
}


def map_for(system: SourceSystem, payload: dict, *, tenant_id: str) -> list[InboundRecord]:
    """Map one inbound payload to its InboundRecord(s), regardless of mapper shape."""
    webhook_mapper = WEBHOOK_MAPPERS.get(system)
    if webhook_mapper is not None:
        return webhook_mapper(payload, tenant_id=tenant_id)
    poll_mapper = POLL_MAPPERS.get(system)
    if poll_mapper is not None:
        return [poll_mapper(payload, tenant_id=tenant_id)]
    raise ValueError(f"no connector mapper registered for {system.value}")
