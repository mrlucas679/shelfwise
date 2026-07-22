from __future__ import annotations

from decimal import Decimal

from .canonical import ExpiryEntry, InventoryState, ProductMaster, SalesLine
from .provenance import ValidationResult


def validate_inventory(item: InventoryState) -> ValidationResult:
    result = ValidationResult()
    if not item.tenant_id:
        result = result.fail("inventory missing tenant_id")
    if not item.sku and not item.gtin and not item.barcode:
        result = result.fail("inventory has no resolvable product identifier")
    if not item.location_id:
        result = result.fail("inventory missing location_id")
    if Decimal(item.quantity) < 0:
        result = result.fail("inventory quantity cannot be negative")
    return result


def validate_product(item: ProductMaster) -> ValidationResult:
    result = ValidationResult()
    if not item.tenant_id:
        result = result.fail("product missing tenant_id")
    if not item.source_product_id:
        result = result.fail("product missing source_product_id")
    if not (item.sku or item.gtin or item.barcode):
        result = result.fail("product has no resolvable identifier")
    if not item.name:
        result = result.warn("product missing name")
    return result


def validate_expiry(item: ExpiryEntry) -> ValidationResult:
    result = ValidationResult()
    if not item.tenant_id:
        result = result.fail("expiry entry missing tenant_id")
    if not item.sku:
        result = result.fail("expiry entry missing sku")
    if not item.location_id:
        result = result.fail("expiry entry missing location_id")
    if Decimal(item.quantity) < 0:
        result = result.fail("expiry entry quantity cannot be negative")
    return result


def validate_sales(item: SalesLine) -> ValidationResult:
    result = ValidationResult()
    if not item.tenant_id:
        result = result.fail("sales line missing tenant_id")
    if not item.order_id:
        result = result.fail("sales line missing order_id")
    if not item.line_id:
        result = result.fail("sales line missing line_id")
    if not item.sku:
        result = result.fail("sales line missing sku")
    if item.quantity <= 0:
        result = result.fail("sales line quantity must be positive")
    if item.unit_price.minor_units < 0:
        result = result.fail("sales line unit_price cannot be negative")
    return result
