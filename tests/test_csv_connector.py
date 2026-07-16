from __future__ import annotations

import asyncio

from shelfwise_contracts import DataDomain, EventType, RecommendedAction, RiskTier
from shelfwise_data import CsvConnector, build_context, build_thresholds


def test_build_context_preserves_money_and_cold_chain_join_keys() -> None:
    context = build_context()
    hero = context["4011"]

    assert hero["on_hand"] == 240
    assert hero["days_to_expiry"] == 3
    assert hero["price"].amount == 30
    assert hero["cost"].amount == 18
    assert hero["category"] == "dairy"
    assert hero["area"] == "store_12"
    assert build_thresholds()["4011"] == 50


def test_csv_connector_read_export_yields_traceable_events() -> None:
    async def run():
        connector = CsvConnector()
        return [event async for event in connector.read_export("stock")]

    events = asyncio.run(run())

    assert events
    first = events[0]
    assert first.type is EventType.STOCK_UPDATE
    assert first.id == "evt_stock_0"
    assert first.tenant_id == "sa_retail_demo"
    assert first.data_domain is DataDomain.WORLD_SIMULATION
    assert first.correlation_id == first.id
    assert first.payload["sku"] == "4011"
    assert len(first.payload["raw_payload_hash"]) == 64


def test_csv_connector_exports_sales_expiry_and_suppliers() -> None:
    async def run():
        connector = CsvConnector()
        sales = [event async for event in connector.read_export("sales")]
        expiry = [event async for event in connector.read_export("expiry")]
        suppliers = [event async for event in connector.read_export("suppliers")]
        return sales, expiry, suppliers

    sales, expiry, suppliers = asyncio.run(run())

    assert sales[0].type is EventType.SALE
    assert sales[0].source.value == "pos_csv"
    assert sales[0].data_domain is DataDomain.WORLD_SIMULATION
    assert expiry[0].type is EventType.EXPIRY_ENTRY
    assert expiry[0].data_domain is DataDomain.WORLD_SIMULATION
    assert expiry[0].payload["expiry_date"] == "2026-07-09"
    assert suppliers[0].type is EventType.SUPPLIER_UPDATE
    assert suppliers[0].data_domain is DataDomain.WORLD_SIMULATION
    assert suppliers[0].payload["supplier"] == "DairyCo"


def test_csv_connector_can_mark_a_real_export_as_operational() -> None:
    async def run():
        connector = CsvConnector(data_domain=DataDomain.OPERATIONAL_TWIN)
        return [event async for event in connector.read_export("stock")]

    events = asyncio.run(run())

    assert events
    assert all(event.data_domain is DataDomain.OPERATIONAL_TWIN for event in events)


def test_csv_connector_rejects_unknown_export_kind() -> None:
    async def run():
        connector = CsvConnector()
        return [event async for event in connector.read_export("nonsense")]

    try:
        asyncio.run(run())
    except ValueError as exc:
        assert "unknown export kind" in str(exc)
    else:
        raise AssertionError("unknown export kind should fail")


def test_csv_connector_write_back_creates_idempotent_task() -> None:
    async def run():
        connector = CsvConnector()
        action = RecommendedAction("apply_markdown", {"sku": "4011"}, RiskTier.HIGH)
        first = await connector.write_back(action, idempotency_key="dec_1")
        second = await connector.write_back(action, idempotency_key="dec_1")
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert first["status"] == "pending_external_write"
    assert first["data_domain"] == DataDomain.WORLD_SIMULATION.value
