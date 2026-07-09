from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Event, EventSource, EventType

from .canonical import InventoryState, SourceSystem, StockState
from .provenance import InboundRecord

_EVENT_SOURCE_BY_SYSTEM = {
    SourceSystem.CSV: EventSource.WMS_CSV,
    SourceSystem.SAP: EventSource.WMS_CSV,
    SourceSystem.SYSPRO: EventSource.WMS_CSV,
    SourceSystem.ODOO: EventSource.WMS_CSV,
    SourceSystem.SQUARE: EventSource.POS_CSV,
    SourceSystem.SHOPIFY: EventSource.POS_CSV,
    SourceSystem.LIGHTSPEED: EventSource.POS_CSV,
}


def inventory_to_event(inventory: InventoryState, record: InboundRecord) -> Event:
    ts = inventory.event_time or record.event_time
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Event(
        id=f"evt_{record.source_system.value}_{record.source_object_type}_{record.source_object_id}",
        type=EventType.STOCK_UPDATE,
        ts=ts,
        actor=record.source_system.value,
        source=_EVENT_SOURCE_BY_SYSTEM.get(record.source_system, EventSource.API),
        tenant_id=record.tenant_id,
        correlation_id=record.correlation_id,
        payload={
            "sku": inventory.sku,
            "location_id": inventory.location_id,
            "quantity": str(inventory.quantity),
            "stock_state": inventory.stock_state.value,
            "source_object_id": record.source_object_id,
            "raw_payload_hash": record.payload_hash,
        },
    )


def record_to_event(record: InboundRecord) -> Event | None:
    if not record.validation.ok:
        return None
    if record.canonical_type == "inventory_state":
        return inventory_to_event(_inventory_from_payload(record), record)
    if record.canonical_type == "sales_line":
        return _sales_line_to_event(record)
    return None


def _inventory_from_payload(record: InboundRecord) -> InventoryState:
    payload = record.canonical_payload
    return InventoryState(
        tenant_id=record.tenant_id,
        sku=str(payload["sku"]),
        location_id=str(payload["location_id"]),
        quantity=Decimal(str(payload.get("quantity", "0"))),
        stock_state=StockState(str(payload.get("stock_state") or StockState.ON_HAND.value)),
        event_time=_datetime_value(payload.get("event_time"), fallback=record.event_time),
        gtin=str(payload["gtin"]) if payload.get("gtin") else None,
        barcode=str(payload["barcode"]) if payload.get("barcode") else None,
    )


def _sales_line_to_event(record: InboundRecord) -> Event:
    payload = record.canonical_payload
    location_id = str(payload.get("location_id") or "online")
    return Event(
        id=_event_id(record),
        type=EventType.SALE,
        ts=_datetime_value(payload.get("sold_at"), fallback=record.event_time),
        actor=location_id,
        source=_EVENT_SOURCE_BY_SYSTEM.get(record.source_system, EventSource.API),
        tenant_id=record.tenant_id,
        correlation_id=record.correlation_id,
        payload={
            "sku": str(payload.get("sku") or ""),
            "location": location_id,
            "location_id": location_id,
            "quantity": int(payload.get("quantity") or 0),
            "unit_price": _unit_price_amount(payload.get("unit_price")),
            "order_id": str(payload.get("order_id") or record.source_object_id),
            "line_id": str(payload.get("line_id") or record.source_object_id),
            "source_object_id": record.source_object_id,
            "raw_payload_hash": record.payload_hash,
        },
    )


def _datetime_value(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = fallback
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _unit_price_amount(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("amount") is not None:
            return str(value["amount"])
        if value.get("minor_units") is not None:
            return str(
                (Decimal(str(value["minor_units"])) / Decimal("100")).quantize(
                    Decimal("0.01")
                )
            )
        return str(value.get("amount") or "0")
    return str(value or "0")


def _event_id(record: InboundRecord) -> str:
    return f"evt_{record.source_system.value}_{record.source_object_type}_{record.source_object_id}"
