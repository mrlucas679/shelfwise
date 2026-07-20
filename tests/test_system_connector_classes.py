from __future__ import annotations

import asyncio
import hashlib
import hmac

from shelfwise_connectors import (
    InMemoryCursorStore,
    InMemoryWebhookDedupStore,
    LightspeedSaleWebhookReceiver,
    OdooProductConnector,
    SapS4InventoryConnector,
    ShopifyOrderWebhookReceiver,
    SourceSystem,
    SquareInventoryWebhookReceiver,
    SysproInventoryConnector,
)


def test_sap_inventory_connector_fetches_pages_and_clears_completed_page_cursor() -> None:
    async def run() -> tuple[list[str], str | None, list[dict[str, str]]]:
        calls: list[dict[str, str]] = []

        async def fetch_json(
            _url: str,
            params: dict[str, str],
            _headers: dict[str, str],
        ) -> dict:
            calls.append(params)
            if len(calls) == 1:
                return {
                    "value": [
                        {
                            "Material": "4011",
                            "StorageLocation": "0001",
                            "MatlWrhsStkQtyInMatlBaseUnit": "240",
                        }
                    ],
                    "@odata.nextLink": "cursor_2",
                }
            return {"value": []}

        cursors = InMemoryCursorStore()
        connector = SapS4InventoryConnector(
            cursors,
            base_url="https://sap.example",
            token="token",
            tenant_id="t1",
            fetch_json=fetch_json,
        )
        ids = [record.source_object_id async for record in connector.pull()]
        cursor = await cursors.get(tenant_id="t1", system=SourceSystem.SAP)
        return ids, cursor, calls

    ids, cursor, calls = asyncio.run(run())

    assert ids == ["4011@0001"]
    assert cursor is None
    assert calls == [{"$top": "200"}, {"$top": "200", "$skiptoken": "cursor_2"}]


def test_odoo_product_connector_builds_jsonrpc_poll_and_uses_write_date_cursor() -> None:
    async def run() -> tuple[list[str], str | None, list[dict]]:
        calls: list[dict] = []

        async def post_json(
            _url: str,
            payload: dict,
            _headers: dict[str, str],
        ) -> dict:
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "result": [
                        {
                            "id": 42,
                            "default_code": "4011",
                            "barcode": "6001234567890",
                            "name": "Yoghurt 1L",
                            "write_date": "2026-07-06 10:00:00",
                        }
                    ]
                }
            return {"result": []}

        cursors = InMemoryCursorStore()
        connector = OdooProductConnector(
            cursors,
            base_url="https://odoo.example",
            database="db",
            uid=7,
            api_key="key",
            tenant_id="t1",
            post_json=post_json,
        )
        ids = [record.source_object_id async for record in connector.pull()]
        cursor = await cursors.get(tenant_id="t1", system=SourceSystem.ODOO)
        return ids, cursor, calls

    ids, cursor, calls = asyncio.run(run())

    assert ids == ["42"]
    assert cursor == "2026-07-06 10:00:00"
    assert calls[0]["params"]["args"][0] == "db"
    assert calls[1]["params"]["args"][5] == [[["write_date", ">", cursor]]]


def test_syspro_inventory_connector_fetches_items_and_clears_completed_page_cursor() -> None:
    async def run() -> tuple[list[str], str | None]:
        async def fetch_json(
            _url: str,
            params: dict[str, str],
            _headers: dict[str, str],
        ) -> dict:
            if "cursor" not in params:
                return {
                    "items": [{"StockCode": "4011", "Warehouse": "WH1", "QtyOnHand": "12"}],
                    "next_cursor": "next",
                }
            return {"items": []}

        cursors = InMemoryCursorStore()
        connector = SysproInventoryConnector(
            cursors,
            base_url="https://syspro.example",
            token="token",
            tenant_id="t1",
            fetch_json=fetch_json,
        )
        ids = [record.source_object_id async for record in connector.pull()]
        cursor = await cursors.get(tenant_id="t1", system=SourceSystem.SYSPRO)
        return ids, cursor

    ids, cursor = asyncio.run(run())

    assert ids == ["4011@WH1"]
    assert cursor is None


def test_source_specific_webhook_receivers_build_inbound_records_and_dedupe() -> None:
    async def run() -> tuple[str, str, str, object]:
        body = b'{"id":"123"}'
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        shopify = ShopifyOrderWebhookReceiver(
            secret="secret",
            dedup=InMemoryWebhookDedupStore(),
            tenant_id="t1",
        )
        square = SquareInventoryWebhookReceiver(
            secret="secret",
            dedup=InMemoryWebhookDedupStore(),
            tenant_id="t1",
        )
        lightspeed = LightspeedSaleWebhookReceiver(
            secret="secret",
            dedup=InMemoryWebhookDedupStore(),
            tenant_id="t1",
        )
        shopify_record = await shopify.receive(
            signature=signature,
            body=body,
            event_id="shopify_123",
            payload={
                "id": 123,
                "created_at": "2026-07-06T10:00:00Z",
                "line_items": [{"id": 1, "sku": "4011", "quantity": 1, "price": "30.00"}],
            },
        )
        square_record = await square.receive(
            signature=signature,
            body=body,
            event_id="square_123",
            payload={
                "data": {
                    "object": {
                        "inventory_counts": [
                            {"catalog_object_id": "4011", "location_id": "L1", "quantity": "3"}
                        ]
                    }
                }
            },
        )
        lightspeed_record = await lightspeed.receive(
            signature=signature,
            body=body,
            event_id="lightspeed_123",
            payload={
                "id": "sale_1",
                "created_at": "2026-07-06T10:00:00Z",
                "lines": [{"id": "line_1", "sku": "4011", "quantity": 1, "price": "30.00"}],
            },
        )
        duplicate = await shopify.receive(
            signature=signature,
            body=body,
            event_id="shopify_123",
            payload={},
        )
        assert shopify_record is not None
        assert square_record is not None
        assert lightspeed_record is not None
        return (
            shopify_record[0].source_system.value,
            square_record[0].source_system.value,
            lightspeed_record[0].source_system.value,
            duplicate,
        )

    shopify_system, square_system, lightspeed_system, duplicate = asyncio.run(run())

    assert shopify_system == "shopify"
    assert square_system == "square"
    assert lightspeed_system == "lightspeed"
    assert duplicate is None
