from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ...canonical import InventoryState, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_inventory
from ..webhook import InMemoryWebhookDedupStore, WebhookReceiver
from ._common import now_utc, wrap


def map_square_inventory(payload: dict, *, tenant_id: str) -> list[InboundRecord]:
    """Map a Square inventory-count webhook to one InboundRecord per count.

    A single webhook can report counts for several catalog objects/locations at once;
    collapsing to a single record would silently drop every count after the first.
    """
    counts = (((payload.get("data") or {}).get("object") or {}).get("inventory_counts")) or []
    if not counts:
        return [
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.SQUARE,
                object_type="inventory_state",
                object_id=str(payload.get("event_id") or "unknown"),
                event_time=now_utc(),
                canonical_type="inventory_state",
                canonical={},
                validation=ValidationResult().fail("square webhook has no inventory_counts"),
                raw=payload,
            )
        ]
    now = now_utc()
    records: list[InboundRecord] = []
    for count in counts:
        catalog_object_id = str(count.get("catalog_object_id") or "unknown")
        location_id = str(count.get("location_id") or "unknown")
        canonical: Any
        try:
            canonical = InventoryState(
                tenant_id=tenant_id,
                sku=catalog_object_id,
                location_id=location_id,
                quantity=Decimal(str(count.get("quantity", 0))),
                event_time=now,
            )
            validation = validate_inventory(canonical)
        except (TypeError, ValueError, InvalidOperation) as exc:
            canonical = {}
            validation = ValidationResult().fail(f"square inventory count is malformed: {exc}")
        records.append(
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.SQUARE,
                object_type="inventory_state",
                object_id=f"{catalog_object_id}@{location_id}",
                event_time=now,
                canonical_type="inventory_state",
                canonical=canonical,
                validation=validation,
                raw=payload,
            )
        )
    return records


class SquareInventoryWebhookReceiver(WebhookReceiver):
    def __init__(
        self,
        *,
        secret: str,
        dedup: InMemoryWebhookDedupStore,
        tenant_id: str,
    ) -> None:
        super().__init__(
            secret=secret,
            dedup=dedup,
            build=lambda payload: map_square_inventory(payload, tenant_id=tenant_id),
        )
