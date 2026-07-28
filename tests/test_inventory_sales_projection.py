from datetime import UTC, datetime

from shelfwise_backend.ingest_pipeline import record_pipeline_event
from shelfwise_backend.state import inventory_position_store
from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_inventory import InMemoryInventoryPositionStore
from shelfwise_runtime import DataDomain


def _event(
    event_id: str,
    event_type: EventType,
    *,
    quantity: object,
    data_domain: DataDomain = DataDomain.OPERATIONAL_TWIN,
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": event_type.value,
        "tenant_id": "tenant_inventory",
        "data_domain": data_domain.value,
        "payload": {
            "sku": "SKU-MILK",
            "location_id": "store-1",
            "quantity": quantity,
        },
    }


def test_sale_projection_decrements_once_and_keeps_a_replay_receipt() -> None:
    store = InMemoryInventoryPositionStore()
    stock = store.project_event(_event("evt_stock", EventType.STOCK_UPDATE, quantity=10))
    sale = store.project_event(_event("evt_sale", EventType.SALE, quantity=3))
    replay = store.project_event(_event("evt_sale", EventType.SALE, quantity=3))

    assert stock["status"] == "stock_position_replaced"
    assert sale["status"] == "sale_applied"
    assert sale["before_quantity"] == 10
    assert sale["after_quantity"] == 7
    assert replay["status"] == "duplicate"
    assert store.list(tenant_id="tenant_inventory")[0]["quantity"] == 7
    assert store.projection_receipt("tenant_inventory", "evt_sale")["after_quantity"] == 7


def test_sale_projection_reports_shortfall_without_negative_stock() -> None:
    store = InMemoryInventoryPositionStore()
    store.project_event(_event("evt_stock", EventType.STOCK_UPDATE, quantity=2))

    receipt = store.project_event(_event("evt_sale", EventType.SALE, quantity=5))

    assert receipt["status"] == "sale_partially_applied"
    assert receipt["after_quantity"] == 0
    assert receipt["unfulfilled_quantity"] == 3
    assert store.list(tenant_id="tenant_inventory")[0]["quantity"] == 0


def test_projection_rejects_fractional_units_and_simulation_state() -> None:
    store = InMemoryInventoryPositionStore()

    fractional = store.project_event(_event("evt_fractional", EventType.SALE, quantity="1.5"))
    simulated = store.project_event(
        _event(
            "evt_simulated",
            EventType.STOCK_UPDATE,
            quantity=10,
            data_domain=DataDomain.WORLD_SIMULATION,
        )
    )

    assert fractional["status"] == "unsupported_quantity"
    assert simulated["status"] == "ignored"
    assert store.list(tenant_id="tenant_inventory") == []


def test_normalized_pos_sale_updates_the_shared_operational_ledger() -> None:
    now = datetime.now(UTC)
    stock_event = Event(
        id="evt_pipeline_stock",
        type=EventType.STOCK_UPDATE,
        ts=now,
        actor="pos",
        source=EventSource.API,
        tenant_id="sa_retail_demo",
        data_domain=DataDomain.OPERATIONAL_TWIN,
        payload={"sku": "SKU-POS", "location_id": "store-pos", "quantity": 8},
    )
    sale_event = Event(
        id="evt_pipeline_sale",
        type=EventType.SALE,
        ts=now,
        actor="pos",
        source=EventSource.POS_CSV,
        tenant_id="sa_retail_demo",
        data_domain=DataDomain.OPERATIONAL_TWIN,
        payload={
            "sku": "SKU-POS",
            "location_id": "store-pos",
            "quantity": 2,
            "unit_price_minor_units": 1999,
        },
    )

    assert record_pipeline_event(stock_event)["inventory"]["status"] == "stock_position_replaced"
    result = record_pipeline_event(sale_event)

    assert result["inventory"]["status"] == "sale_applied"
    positions = inventory_position_store.list(tenant_id="sa_retail_demo", sku="SKU-POS")
    assert positions[0]["quantity"] == 6
