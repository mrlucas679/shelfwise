from __future__ import annotations

import asyncio

from shelfwise_connectors import (
    DynamicsBusinessCentralInventoryConnector,
    InMemoryCursorStore,
    SourceSystem,
    map_dynamics_inventory,
    map_for,
    map_yoco_checkout,
)
from shelfwise_connectors.normalize import record_to_event
from shelfwise_contracts import EventSource


def test_dynamics_item_maps_to_inventory_snapshot() -> None:
    record = map_dynamics_inventory(
        {
            "id": "item-guid",
            "number": "1000",
            "inventory": 12.5,
            "gtin": "6001234567890",
            "lastModifiedDateTime": "2026-07-20T12:00:00Z",
        },
        tenant_id="tenant-a",
        location_id="warehouse-1",
    )

    assert record.source_system is SourceSystem.DYNAMICS
    assert record.validation.ok is True
    assert record.source_object_id == "item-guid@warehouse-1"
    assert record.canonical_payload["sku"] == "1000"
    assert record.canonical_payload["quantity"] == "12.5"


def test_dynamics_and_yoco_malformed_timestamps_are_quarantined() -> None:
    dynamics = map_dynamics_inventory(
        {"id": "item-guid", "number": "1000", "inventory": 2, "lastModifiedDateTime": "nope"},
        tenant_id="tenant-a",
        location_id="warehouse-1",
    )
    yoco = map_yoco_checkout(
        {
            "id": "ch_123",
            "status": "succeeded",
            "amount": 100,
            "createdDate": "nope",
            "metadata": {"sku": "SKU"},
        },
        tenant_id="tenant-a",
    )

    assert dynamics.validation.ok is False
    assert dynamics.validation.errors == ("dynamics item lastModifiedDateTime is malformed",)
    assert yoco[0].validation.ok is False
    assert yoco[0].validation.errors == ("yoco checkout timestamp is malformed",)


def test_dynamics_connector_reuses_opaque_odata_next_link() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(url: str, params: dict[str, str], _: dict[str, str]) -> dict:
        calls.append((url, params))
        if len(calls) == 1:
            return {
                "value": [{"id": "one", "number": "1000", "inventory": 2}],
                "@odata.nextLink": "https://bc.example/items?$skiptoken=opaque",
            }
        return {"value": [{"id": "two", "number": "1001", "inventory": 3}]}

    async def pull() -> list[str]:
        connector = DynamicsBusinessCentralInventoryConnector(
            InMemoryCursorStore(),
            base_url="https://bc.example/items",
            token="token",
            location_id="store-1",
            tenant_id="tenant-a",
            fetch_json=fetch,
        )
        return [record.source_object_id async for record in connector.pull()]

    assert asyncio.run(pull()) == ["one@store-1", "two@store-1"]
    assert calls == [
        ("https://bc.example/items", {"$top": "200"}),
        ("https://bc.example/items?$skiptoken=opaque", {}),
    ]


def test_yoco_succeeded_checkout_requires_reconciled_retail_metadata() -> None:
    valid = map_yoco_checkout(
        {
            "id": "ch_123",
            "status": "succeeded",
            "amount": 12_500,
            "currency": "ZAR",
            "createdDate": "2026-07-20T12:00:00Z",
            "metadata": {"sku": "MILK-2L", "quantity": 2, "location_id": "store-1"},
        },
        tenant_id="tenant-a",
    )
    missing_sku = map_yoco_checkout(
        {"id": "ch_124", "status": "succeeded", "amount": 100, "metadata": {}},
        tenant_id="tenant-a",
    )

    assert valid[0].source_system is SourceSystem.YOCO
    assert valid[0].validation.ok is True
    assert valid[0].canonical_payload["unit_price"]["minor_units"] == 12_500
    assert missing_sku[0].validation.ok is False
    assert missing_sku[0].validation.errors == ("yoco checkout metadata has no sku",)


def test_registry_routes_dynamics_and_yoco() -> None:
    dynamics = map_for(
        SourceSystem.DYNAMICS,
        {"id": "item-guid", "number": "1000", "inventory": 4, "location_id": "store-1"},
        tenant_id="tenant-a",
    )
    yoco = map_for(
        SourceSystem.YOCO,
        {"id": "ch_123", "status": "succeeded", "amount": 100, "metadata": {"sku": "SKU"}},
        tenant_id="tenant-a",
    )

    assert dynamics[0].validation.ok is True
    assert yoco[0].validation.ok is True
    assert record_to_event(dynamics[0]).source is EventSource.WMS_CSV
    assert record_to_event(yoco[0]).source is EventSource.POS_CSV


def test_manual_dynamics_intake_refuses_to_invent_a_location() -> None:
    records = map_for(
        SourceSystem.DYNAMICS,
        {"id": "item-guid", "number": "1000", "inventory": 4},
        tenant_id="tenant-a",
    )

    assert records[0].validation.ok is False
