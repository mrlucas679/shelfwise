from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from shelfwise_backend.app import _project_twin_event
from shelfwise_backend.tools.mcp_surface import (
    AuditLog,
    build_live_twin_tools,
    build_platform_tools,
)
from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_runtime.provenance import DataDomain, DataDomainBoundaryError
from shelfwise_twin import InMemoryTwinStore, StateLane, TwinObservation, TwinService


class _Memory:
    def __init__(self) -> None:
        self.scopes: list[tuple[str | None, str | None]] = []

    def thresholds(self, tenant_id=None, data_domain=None):
        self.scopes.append((tenant_id, data_domain))
        return {"expiry_days": 3}


class _Decisions:
    def list(self):
        return []

    def get(self, _decision_id):
        return None


def test_simulation_event_is_skipped_by_twin_projection() -> None:
    event = Event(
        id="evt_demo_temperature",
        type=EventType.COLD_CHAIN_ALERT,
        ts=datetime(2026, 7, 13, 8, tzinfo=UTC),
        actor="simulator",
        source=EventSource.API,
        tenant_id="tenant_a",
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id="demo_cold_chain_1",
        payload={"store_id": "store_1", "synthetic": True, "asset_id": "fridge_1"},
    )

    result = _project_twin_event(event)

    assert result["status"] == "skipped_non_operational"
    assert result["data_domain"] == "world_simulation"


def test_twin_service_itself_rejects_simulation_events() -> None:
    service = TwinService(InMemoryTwinStore())
    event = Event(
        id="evt_world_direct",
        type=EventType.COLD_CHAIN_ALERT,
        ts=datetime(2026, 7, 13, 8, tzinfo=UTC),
        actor="simulator",
        tenant_id="tenant_a",
        data_domain=DataDomain.WORLD_SIMULATION,
        payload={"store_id": "store_1", "asset_id": "fridge_1"},
    )

    with pytest.raises(DataDomainBoundaryError, match="operational twin projection"):
        service.project_event(event)

    assert service.store.list_entities("tenant_a") == []


def test_live_tools_return_reported_twin_value_only() -> None:
    service = TwinService(InMemoryTwinStore())
    value = 25
    digest = sha256(b"live-fridge").hexdigest()
    service.accept(
        TwinObservation(
            observation_id="obs_live_fridge_1",
            tenant_id="tenant_a",
            store_id="store_1",
            twin_id="urn:shelfwise:tenant_a:store_1:asset:fridge_1",
            property_name="cold_chain.temperature_c",
            lane=StateLane.REPORTED,
            value=value,
            observed_at=datetime(2026, 7, 13, 8, tzinfo=UTC),
            source_system="api",
            source_object_id="device-fridge-1",
            source_quality=1.0,
            correlation_id="live-1",
            payload_hash=digest,
        )
    )
    tools = build_live_twin_tools(
        decisions=_Decisions(), memory=_Memory(), twin=service, tenant_id="tenant_a"
    )
    tool = next(item for item in tools if item.name == "get_live_twin_state")

    result = asyncio.run(tool.fn(store_id="store_1"))

    assert result["data_domain"] == "operational_twin"
    assert result["synthetic"] is False
    assert result["properties"][0]["value"] == value


def test_live_stock_filters_exact_sku_before_response_limit() -> None:
    service = TwinService(InMemoryTwinStore())
    observed_at = datetime.now(UTC)
    for index, (sku, value) in enumerate((("SKU-A", 11), ("SKU-B", 29)), start=1):
        service.accept(
            TwinObservation(
                observation_id=f"obs_live_stock_{index}",
                tenant_id="tenant_a",
                store_id="store_1",
                twin_id=f"urn:shelfwise:tenant_a:store_1:product:{sku}",
                property_name="inventory.on_hand",
                lane=StateLane.REPORTED,
                value=value,
                observed_at=observed_at,
                source_system="api",
                source_object_id=f"stock-{sku}",
                source_quality=1.0,
                correlation_id=f"stock-{index}",
                payload_hash=sha256(f"stock-{sku}".encode()).hexdigest(),
            )
        )
    memory = _Memory()
    tools = build_live_twin_tools(
        decisions=_Decisions(), memory=memory, twin=service, tenant_id="tenant_a"
    )
    stock_tool = next(item for item in tools if item.name == "get_live_stock")
    threshold_tool = next(item for item in tools if item.name == "live_get_thresholds")

    stock = asyncio.run(stock_tool.fn(sku="SKU-B", store_id="store_1"))
    thresholds = asyncio.run(threshold_tool.fn())

    assert [item["value"] for item in stock["properties"]] == [29]
    assert stock["requested_sku"] == "SKU-B"
    assert thresholds["thresholds"] == {"expiry_days": 3}
    assert memory.scopes == [("tenant_a", "operational_twin")]


def test_shared_agent_tools_follow_operational_facts_domain() -> None:
    class Decisions:
        def list(self):
            return [
                {
                    "id": "dec_live",
                    "tenant_id": "tenant_a",
                    "data_domain": "operational_twin",
                    "status": "pending",
                },
                {
                    "id": "dec_sim",
                    "tenant_id": "tenant_a",
                    "data_domain": "world_simulation",
                    "status": "pending",
                },
            ]

        def get(self, decision_id):
            return next(
                (item for item in self.list() if item["id"] == decision_id),
                None,
            )

    memory = _Memory()
    audit = AuditLog()
    tools = build_platform_tools(
        decisions=Decisions(),
        memory=memory,
        facts=SimpleNamespace(data_domain="operational_twin"),
        tenant_id="tenant_a",
        audit=audit,
    )
    threshold_tool = next(item for item in tools if item.name == "get_thresholds")
    decision_tool = next(item for item in tools if item.name == "list_open_decisions")

    thresholds = asyncio.run(threshold_tool.fn())
    decisions = asyncio.run(decision_tool.fn())

    assert thresholds["data_domain"] == "operational_twin"
    assert memory.scopes == [("tenant_a", "operational_twin")]
    assert [item["id"] for item in decisions["decisions"]] == ["dec_live"]
    assert {item["data_domain"] for item in audit.list()} == {"operational_twin"}
