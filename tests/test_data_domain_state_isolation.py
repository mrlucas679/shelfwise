from __future__ import annotations

from datetime import UTC, date, datetime

from shelfwise_backend.candidate_factory import generate_fleet_candidates
from shelfwise_backend.candidate_store import InMemoryCandidateStore
from shelfwise_backend.open_orders import InMemoryOpenOrderStore
from shelfwise_backend.trace import TraceRegistry
from shelfwise_connectors.writeback import TaskWriteBackSink
from shelfwise_contracts import Event, EventType
from shelfwise_memory import InMemoryLearningStore
from shelfwise_runtime import DataDomain


def _shipment(domain: DataDomain, units: int) -> Event:
    return Event(
        id="evt_shared_shipment",
        type=EventType.SHIPMENT,
        ts=datetime(2026, 7, 13, 10, tzinfo=UTC),
        actor="wms",
        tenant_id="tenant_a",
        data_domain=domain,
        payload={
            "order_id": "PO-SHARED",
            "sku": "SKU-1",
            "ordered_units": units,
        },
    )


def _approved_decision(domain: str, exposure: int) -> dict:
    return {
        "id": f"dec_{domain}",
        "tenant_id": "tenant_a",
        "data_domain": domain,
        "status": "approved",
        "action": {
            "type": "review_expiry_markdown",
            "params": {"sku": "SKU-1"},
        },
        "expected_outcome": {"days_to_expiry": exposure},
    }


def test_learning_and_open_orders_are_partitioned_by_data_domain() -> None:
    learning = InMemoryLearningStore()
    orders = InMemoryOpenOrderStore()
    learning.record_approved_decision(_approved_decision("world_simulation", 2))
    learning.record_approved_decision(_approved_decision("operational_twin", 5))
    orders.observe_event(_shipment(DataDomain.WORLD_SIMULATION, 12))
    orders.observe_event(_shipment(DataDomain.OPERATIONAL_TWIN, 30))

    world_thresholds = learning.thresholds(
        tenant_id="tenant_a", data_domain="world_simulation"
    )
    live_thresholds = learning.thresholds(
        tenant_id="tenant_a", data_domain="operational_twin"
    )

    assert world_thresholds["SKU-1:expiry_review_days_to_expiry"] == 2
    assert live_thresholds["SKU-1:expiry_review_days_to_expiry"] == 5
    assert orders.coverage(
        "tenant_a", data_domain="world_simulation"
    )["SKU-1"]["remaining_units"] == 12
    assert orders.coverage(
        "tenant_a", data_domain="operational_twin"
    )["SKU-1"]["remaining_units"] == 30


def test_candidates_traces_and_writeback_are_partitioned_by_data_domain() -> None:
    candidate_store = InMemoryCandidateStore()
    trace_registry = TraceRegistry()
    writeback = TaskWriteBackSink()
    base_item = {
        "sku": "SKU-1",
        "name": "Milk",
        "category": "dairy",
        "supplier": "Supplier",
        "unit_price": 20,
        "unit_cost": 10,
        "on_hand": 1,
        "reorder_point": 10,
        "expiry_date": "2026-07-15",
        "days_to_expiry": 2,
        "attention_reasons": ["low_stock"],
        "batches": [],
    }
    candidates = []
    for domain in ("world_simulation", "operational_twin"):
        candidates.extend(
            generate_fleet_candidates(
                [{**base_item, "data_domain": domain}],
                tenant_id="tenant_a",
                as_of=date(2026, 7, 13),
                limit=10,
            )
        )
        trace_registry.put(
            {
                "correlation_id": "shared-correlation",
                "tenant_id": "tenant_a",
                "data_domain": domain,
                "scenario": "test",
                "evidence": [],
                "trace": [],
            }
        )
        writeback.create_task(
            idempotency_key="shared-key",
            tenant_id="tenant_a",
            data_domain=domain,
            title="Review stock",
            action={"type": "review"},
        )
    candidate_store.upsert_many(candidates)

    world_candidates = candidate_store.list(
        "tenant_a", data_domain="world_simulation"
    )
    live_candidates = candidate_store.list(
        "tenant_a", data_domain="operational_twin"
    )

    assert world_candidates and live_candidates
    assert {item["candidate_key"] for item in world_candidates}.isdisjoint(
        {item["candidate_key"] for item in live_candidates}
    )
    assert trace_registry.get(
        "shared-correlation",
        tenant_id="tenant_a",
        data_domain="world_simulation",
    )["data_domain"] == "world_simulation"
    assert len(writeback.list(tenant_id="tenant_a", data_domain="operational_twin")) == 1
    assert len(writeback.list(tenant_id="tenant_a", data_domain="world_simulation")) == 1
