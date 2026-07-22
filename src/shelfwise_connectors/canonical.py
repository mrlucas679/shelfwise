from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from shelfwise_contracts import Money


class SourceSystem(StrEnum):
    CSV = "csv"
    SAP = "sap"
    SYSPRO = "syspro"
    ODOO = "odoo"
    DYNAMICS = "dynamics"
    SHOPIFY = "shopify"
    SQUARE = "square"
    LIGHTSPEED = "lightspeed"
    YOCO = "yoco"


class StockState(StrEnum):
    ON_HAND = "on_hand"
    RESERVED = "reserved"
    DAMAGED = "damaged"
    EXPIRED = "expired"
    IN_TRANSIT = "in_transit"


@dataclass(frozen=True, slots=True)
class ProductMaster:
    tenant_id: str
    source_system: SourceSystem
    source_product_id: str
    sku: str | None = None
    gtin: str | None = None
    barcode: str | None = None
    name: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class SalesLine:
    tenant_id: str
    order_id: str
    line_id: str
    sku: str
    location_id: str
    quantity: int
    unit_price: Money
    sold_at: datetime


@dataclass(frozen=True, slots=True)
class ExpiryEntry:
    """A batch-on-hand observation with its expiry date, per SKU and location.

    Kept separate from `InventoryState` because expiry is a distinct observed fact with
    its own event type and evidence rules — folding it into the stock row would make
    "no expiry data" indistinguishable from "not perishable".
    """

    tenant_id: str
    sku: str
    location_id: str
    quantity: Decimal
    expiry_date: date


@dataclass(frozen=True, slots=True)
class InventoryState:
    tenant_id: str
    sku: str
    location_id: str
    quantity: Decimal
    stock_state: StockState = StockState.ON_HAND
    event_time: datetime | None = None
    gtin: str | None = None
    barcode: str | None = None
