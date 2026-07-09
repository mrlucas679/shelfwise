from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from ...canonical import SalesLine, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_sales
from ..webhook import InMemoryWebhookDedupStore, WebhookReceiver
from ._common import parse_time, wrap


def map_lightspeed_sale(payload: dict, *, tenant_id: str) -> list[InboundRecord]:
    """Map a Lightspeed sale webhook to one InboundRecord per sale line.

    A multi-line sale carries independent sales facts per line (sku, quantity, price);
    collapsing to a single record would silently undercount every line after the first.
    """
    order_id = str(payload.get("id") or payload.get("sale_id") or "unknown")
    sold_at = parse_time(payload.get("created_at") or payload.get("sale_time"))
    line_items = _line_items(payload)
    if not line_items:
        return [
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.LIGHTSPEED,
                object_type="sales_line",
                object_id=order_id,
                event_time=sold_at,
                canonical_type="sales_line",
                canonical={},
                validation=ValidationResult().fail("lightspeed sale has no line_items"),
                raw=payload,
            )
        ]
    location_id = str(
        payload.get("location_id") or payload.get("shop_id") or payload.get("register_id")
        or "store"
    )
    records: list[InboundRecord] = []
    for line in line_items:
        line_id = (
            str(line.get("id") or line.get("line_id") or line.get("product_id") or "unknown")
            if isinstance(line, dict)
            else "unknown"
        )
        canonical: Any
        try:
            canonical = SalesLine(
                tenant_id=tenant_id,
                order_id=order_id,
                line_id=line_id,
                sku=str(line.get("sku") or line.get("custom_sku") or line.get("product_id") or ""),
                location_id=location_id,
                quantity=int(line.get("quantity") or line.get("qty") or 0),
                unit_price=Money.zar(line.get("price") or line.get("unit_price") or "0"),
                sold_at=sold_at,
            )
            validation = validate_sales(canonical)
        except (TypeError, ValueError, InvalidOperation) as exc:
            canonical = {}
            validation = ValidationResult().fail(f"lightspeed line item is malformed: {exc}")
        records.append(
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.LIGHTSPEED,
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


class LightspeedSaleWebhookReceiver(WebhookReceiver):
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
            build=lambda payload: map_lightspeed_sale(payload, tenant_id=tenant_id),
        )


def _line_items(payload: dict) -> list[dict]:
    raw = payload.get("line_items") or payload.get("lines") or payload.get("saleLines") or []
    return raw if isinstance(raw, list) else []
