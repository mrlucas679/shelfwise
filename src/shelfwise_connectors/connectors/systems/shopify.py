from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from ...canonical import SalesLine, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_sales
from ..webhook import InMemoryWebhookDedupStore, WebhookReceiver
from ._common import parse_quantity, parse_time, wrap


def map_shopify_order(payload: dict, *, tenant_id: str) -> list[InboundRecord]:
    """Map a Shopify order webhook to one InboundRecord per line item.

    A multi-item order carries independent sales facts per line (sku, quantity, price);
    collapsing to a single record would silently undercount every line after the first.
    """
    order_id = str(payload.get("id", "unknown"))
    line_items = payload.get("line_items") or []
    if not line_items:
        return [
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.SHOPIFY,
                object_type="sales_line",
                object_id=order_id,
                event_time=parse_time(payload.get("created_at")),
                canonical_type="sales_line",
                canonical={},
                validation=ValidationResult().fail("shopify order has no line_items"),
                raw=payload,
            )
        ]
    sold_at = parse_time(payload.get("created_at"))
    location_id = str(payload.get("location_id") or "online")
    records: list[InboundRecord] = []
    for line in line_items:
        line_id = str(line.get("id", "unknown")) if isinstance(line, dict) else "unknown"
        canonical: Any
        try:
            canonical = SalesLine(
                tenant_id=tenant_id,
                order_id=order_id,
                line_id=line_id,
                sku=str(line.get("sku") or line.get("product_id") or ""),
                location_id=location_id,
                quantity=parse_quantity(line.get("quantity", 0)),
                unit_price=Money.zar(line.get("price", "0")),
                sold_at=sold_at,
            )
            validation = validate_sales(canonical)
        except (TypeError, ValueError, InvalidOperation) as exc:
            canonical = {}
            validation = ValidationResult().fail(f"shopify line item is malformed: {exc}")
        records.append(
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.SHOPIFY,
                object_type="sales_line",
                object_id=f"{order_id}:{line_id}",
                event_time=sold_at,
                canonical_type="sales_line",
                canonical=canonical,
                validation=validation,
                raw=payload,
            )
        )
    return records


class ShopifyOrderWebhookReceiver(WebhookReceiver):
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
            build=lambda payload: map_shopify_order(payload, tenant_id=tenant_id),
        )
