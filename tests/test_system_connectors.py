from __future__ import annotations

from decimal import Decimal

import pytest

from shelfwise_connectors import (
    InventoryState,
    SourceSystem,
    inventory_to_event,
    map_for,
    map_lightspeed_sale,
    map_odoo_product,
    map_sap_inventory,
    map_shopify_order,
    map_square_inventory,
    map_syspro_inventory,
    record_to_event,
)


def test_sap_inventory_row_maps_to_valid_inventory_record() -> None:
    rec = map_sap_inventory(
        {
            "Material": "4011",
            "StorageLocation": "0001",
            "MatlWrhsStkQtyInMatlBaseUnit": "240",
        },
        tenant_id="t1",
    )

    assert rec.source_system is SourceSystem.SAP
    assert rec.source_object_id == "4011@0001"
    assert rec.canonical_type == "inventory_state"
    assert rec.canonical_payload["quantity"] == "240"
    assert rec.validation.ok is True


def test_odoo_product_maps_identifiers() -> None:
    rec = map_odoo_product(
        {"id": 42, "default_code": "4011", "barcode": "6001234567890", "name": "Yoghurt 1L"},
        tenant_id="t1",
    )

    assert rec.source_system is SourceSystem.ODOO
    assert rec.canonical_type == "product"
    assert rec.canonical_payload["sku"] == "4011"
    assert rec.canonical_payload["gtin"] == "6001234567890"
    assert rec.validation.ok is True


def test_syspro_inventory_row_maps_to_valid_inventory_record() -> None:
    rec = map_syspro_inventory(
        {"StockCode": "4011", "Warehouse": "0001", "QtyOnHand": "240"},
        tenant_id="t1",
    )

    assert rec.source_system is SourceSystem.SYSPRO
    assert rec.source_object_id == "4011@0001"
    assert rec.canonical_type == "inventory_state"
    assert rec.canonical_payload["quantity"] == "240"
    assert rec.validation.ok is True


def test_shopify_order_maps_to_sales_line_money() -> None:
    records = map_shopify_order(
        {
            "id": 1234,
            "created_at": "2026-07-06T10:00:00Z",
            "location_id": 99,
            "line_items": [{"id": 1, "sku": "4011", "quantity": 2, "price": "20.00"}],
        },
        tenant_id="t1",
    )

    assert len(records) == 1
    rec = records[0]
    assert rec.source_system is SourceSystem.SHOPIFY
    assert rec.source_object_id == "1234:1"
    assert rec.canonical_type == "sales_line"
    assert rec.canonical_payload["quantity"] == 2
    assert rec.canonical_payload["unit_price"]["minor_units"] == 2000
    assert rec.canonical_payload["unit_price"]["currency"] == "ZAR"
    assert rec.validation.ok is True


def test_shopify_multi_line_order_maps_every_line_not_just_the_first() -> None:
    records = map_shopify_order(
        {
            "id": 5001,
            "created_at": "2026-07-06T10:00:00Z",
            "location_id": 99,
            "line_items": [
                {"id": 1, "sku": "4011", "quantity": 2, "price": "20.00"},
                {"id": 2, "sku": "4022", "quantity": 5, "price": "9.99"},
            ],
        },
        tenant_id="t1",
    )

    assert len(records) == 2
    assert [rec.source_object_id for rec in records] == ["5001:1", "5001:2"]
    assert [rec.canonical_payload["sku"] for rec in records] == ["4011", "4022"]
    assert all(rec.validation.ok for rec in records)


def test_lightspeed_sale_maps_to_sales_line_and_normalizes() -> None:
    records = map_lightspeed_sale(
        {
            "id": "sale_1",
            "created_at": "2026-07-06T10:00:00Z",
            "shop_id": "store_12",
            "lines": [{"id": "line_1", "sku": "4011", "quantity": 2, "price": "30.00"}],
        },
        tenant_id="t1",
    )

    assert len(records) == 1
    rec = records[0]
    event = record_to_event(rec)

    assert rec.source_system is SourceSystem.LIGHTSPEED
    assert rec.canonical_type == "sales_line"
    assert rec.validation.ok is True
    assert event is not None
    assert event.type.value == "sale"
    assert event.payload["unit_price"] == "30.00"


def test_square_inventory_webhook_maps_and_normalizes_to_event() -> None:
    records = map_square_inventory(
        {
            "type": "inventory.count.updated",
            "data": {
                "object": {
                    "inventory_counts": [
                        {"catalog_object_id": "sq_4011", "location_id": "L1", "quantity": "240"}
                    ]
                }
            },
        },
        tenant_id="t1",
    )

    assert len(records) == 1
    rec = records[0]
    assert rec.source_system is SourceSystem.SQUARE
    assert rec.source_object_id == "sq_4011@L1"
    assert rec.canonical_payload["quantity"] == "240"
    event = inventory_to_event(
        inventory=InventoryState(
            tenant_id="t1",
            sku=rec.canonical_payload["sku"],
            location_id=rec.canonical_payload["location_id"],
            quantity=Decimal(str(rec.canonical_payload["quantity"])),
        ),
        record=rec,
    )
    assert event.tenant_id == "t1"
    assert event.payload["sku"] == "sq_4011"


def test_square_webhook_maps_every_inventory_count_not_just_the_first() -> None:
    records = map_square_inventory(
        {
            "data": {
                "object": {
                    "inventory_counts": [
                        {"catalog_object_id": "sq_1", "location_id": "L1", "quantity": "5"},
                        {"catalog_object_id": "sq_2", "location_id": "L2", "quantity": "9"},
                    ]
                }
            },
        },
        tenant_id="t1",
    )

    assert len(records) == 2
    assert [rec.source_object_id for rec in records] == ["sq_1@L1", "sq_2@L2"]
    assert all(rec.validation.ok for rec in records)


def test_registry_routes_to_registered_mapper_and_rejects_unsupported_system() -> None:
    payload = {
        "type": "inventory.count.updated",
        "data": {
            "object": {
                "inventory_counts": [
                    {"catalog_object_id": "sq_1", "location_id": "L1", "quantity": "5"}
                ]
            }
        },
    }

    mapped = map_for(SourceSystem.SQUARE, payload, tenant_id="t1")

    assert len(mapped) == 1
    assert mapped[0].source_system is SourceSystem.SQUARE
    with pytest.raises(ValueError, match="no connector mapper registered"):
        map_for(SourceSystem.DYNAMICS, {}, tenant_id="t1")


def test_registry_wraps_single_record_poll_mappers_in_a_list() -> None:
    mapped = map_for(
        SourceSystem.SAP,
        {
            "Material": "4011",
            "StorageLocation": "0001",
            "MatlWrhsStkQtyInMatlBaseUnit": "240",
        },
        tenant_id="t1",
    )

    assert len(mapped) == 1
    assert mapped[0].source_system is SourceSystem.SAP


def test_bad_webhook_payloads_return_invalid_records_not_exceptions() -> None:
    shopify = map_shopify_order({"id": 1, "created_at": "2026-07-06T10:00:00Z"}, tenant_id="t1")
    square = map_square_inventory({"event_id": "e1", "data": {"object": {}}}, tenant_id="t1")

    assert len(shopify) == 1
    assert shopify[0].validation.ok is False
    assert "shopify order has no line_items" in shopify[0].validation.errors
    assert len(square) == 1
    assert square[0].validation.ok is False
    assert "square webhook has no inventory_counts" in square[0].validation.errors


def test_bad_syspro_and_lightspeed_payloads_return_invalid_records() -> None:
    syspro = map_syspro_inventory({"QtyOnHand": "1"}, tenant_id="t1")
    lightspeed = map_lightspeed_sale({"id": "sale_1"}, tenant_id="t1")

    assert syspro.validation.ok is False
    assert "syspro inventory row has no stock code or warehouse" in syspro.validation.errors
    assert len(lightspeed) == 1
    assert lightspeed[0].validation.ok is False
    assert "lightspeed sale has no line_items" in lightspeed[0].validation.errors
