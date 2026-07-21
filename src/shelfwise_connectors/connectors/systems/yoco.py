"""Yoco checkout-webhook mapping with explicit retail-line metadata requirements."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from ...canonical import SalesLine, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_sales
from ..webhook import InMemoryWebhookDedupStore, WebhookReceiver
from ._common import parse_quantity, parse_time, wrap


def map_yoco_checkout(payload: dict, *, tenant_id: str) -> list[InboundRecord]:
    """Map a succeeded Yoco checkout into one sale only when retail metadata is explicit.

    Payment processors know the money movement but not a retailer's SKU/location grain.
    Requiring caller-owned metadata prevents a successful payment from being mistaken for
    an inventory sale when the checkout has not been reconciled to a catalogue line.
    """
    checkout = _checkout_object(payload)
    checkout_id = str(checkout.get("id") or payload.get("id") or "unknown")
    try:
        sold_at = parse_time(checkout.get("createdDate") or checkout.get("created_at"))
    except (TypeError, ValueError):
        return [
            _invalid(
                payload,
                tenant_id,
                checkout_id,
                parse_time(None),
                "yoco checkout timestamp is malformed",
            )
        ]
    metadata = checkout.get("metadata")
    if not isinstance(metadata, dict):
        return [_invalid(payload, tenant_id, checkout_id, sold_at, "yoco checkout has no metadata")]
    status = str(checkout.get("status") or "").strip().lower()
    if status not in {"succeeded", "completed"}:
        return [
            _invalid(payload, tenant_id, checkout_id, sold_at, "yoco checkout is not succeeded")
        ]
    sku = str(metadata.get("sku") or "").strip()
    if not sku:
        return [
            _invalid(payload, tenant_id, checkout_id, sold_at, "yoco checkout metadata has no sku")
        ]
    try:
        amount_minor = int(checkout.get("amount"))
        if amount_minor < 0:
            raise ValueError("amount must be non-negative")
        quantity = parse_quantity(metadata.get("quantity", 1))
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        unit_price_minor = _exact_unit_price_minor(amount_minor, quantity)
        sale = SalesLine(
            tenant_id=tenant_id,
            order_id=str(metadata.get("order_id") or checkout_id),
            line_id=str(metadata.get("line_id") or checkout_id),
            sku=sku,
            location_id=str(metadata.get("location_id") or "online"),
            quantity=quantity,
            unit_price=Money(
                minor_units=unit_price_minor,
                currency=str(checkout.get("currency") or "ZAR"),
            ),
            sold_at=sold_at,
        )
        validation = validate_sales(sale)
        canonical: Any = sale
    except (InvalidOperation, TypeError, ValueError) as exc:
        return [
            _invalid(
                payload,
                tenant_id,
                checkout_id,
                sold_at,
                f"yoco checkout is malformed: {exc}",
            )
        ]
    return [
        wrap(
            tenant_id=tenant_id,
            system=SourceSystem.YOCO,
            object_type="sales_line",
            object_id=checkout_id,
            event_time=sold_at,
            canonical_type="sales_line",
            canonical=canonical,
            validation=validation,
            raw=payload,
        )
    ]


def _exact_unit_price_minor(amount_minor: int, quantity: int | Decimal) -> int:
    """Return an exactly representable unit price for a checkout-total amount.

    Yoco's checkout amount is the money movement for the whole checkout, not a line-unit
    price. ShelfWise's SalesLine contract stores a unit price in integral minor units, so
    a non-exact split must be quarantined instead of rounding and corrupting revenue.
    """
    unit_price = Decimal(amount_minor) / Decimal(quantity)
    if not unit_price.is_finite() or unit_price != unit_price.to_integral_value():
        raise ValueError("checkout total cannot be represented as an exact unit price")
    return int(unit_price)


class YocoCheckoutWebhookReceiver(WebhookReceiver):
    """Authenticate/dedupe a configured Yoco checkout delivery before mapping it."""

    def __init__(self, *, secret: str, dedup: InMemoryWebhookDedupStore, tenant_id: str) -> None:
        super().__init__(
            secret=secret,
            dedup=dedup,
            build=lambda payload: map_yoco_checkout(payload, tenant_id=tenant_id),
        )


def _checkout_object(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("object"), dict):
        return data["object"]
    return payload


def _invalid(
    payload: dict,
    tenant_id: str,
    checkout_id: str,
    sold_at: object,
    error: str,
) -> InboundRecord:
    timestamp = sold_at if hasattr(sold_at, "tzinfo") else parse_time(None)
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.YOCO,
        object_type="sales_line",
        object_id=checkout_id,
        event_time=timestamp,
        canonical_type="sales_line",
        canonical={},
        validation=ValidationResult().fail(error),
        raw=payload,
    )
