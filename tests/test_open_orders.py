from __future__ import annotations

from shelfwise_backend.open_orders import InMemoryOpenOrderStore
from shelfwise_contracts import Event


def _shipment(
    tenant_id: str,
    *,
    received_units: int = 0,
    order_id: str = "PO-1",
    ts: str = "2026-07-13T10:00:00Z",
    eta: str = "2026-07-15T10:00:00Z",
) -> Event:
    return Event.parse_wire(
        {
            "id": f"shipment-{tenant_id}-{received_units}",
            "type": "shipment",
            "ts": ts,
            "actor": "wms",
            "source": "api",
            "tenant_id": tenant_id,
            "payload": {
                "order_id": order_id,
                "sku": "SKU-1",
                "supplier_id": "supplier-1",
                "ordered_units": 20,
                "received_units": received_units,
                "eta": eta,
            },
        }
    )


def test_open_order_store_is_idempotent_and_aggregates_remaining_units() -> None:
    store = InMemoryOpenOrderStore()

    first = store.observe_event(_shipment("tenant-a"))
    repeated = store.observe_event(_shipment("tenant-a"))

    assert first is not None
    assert repeated is not None
    assert first["remaining_units"] == 20
    assert store.coverage("tenant-a")["SKU-1"]["remaining_units"] == 20
    assert store.coverage("tenant-b") == {}


def test_receipt_closes_open_order_without_affecting_another_tenant() -> None:
    store = InMemoryOpenOrderStore()
    store.observe_event(_shipment("tenant-a"))

    fulfilled = store.observe_event(_shipment("tenant-a", received_units=20))

    assert fulfilled is not None
    assert fulfilled["status"] == "fulfilled"
    assert store.coverage("tenant-a") == {}


def test_coverage_includes_more_than_the_order_list_page() -> None:
    store = InMemoryOpenOrderStore()
    for index in range(501):
        store.observe_event(
            _shipment("tenant-a", order_id=f"PO-{index}", eta="2026-07-20T10:00:00Z")
        )

    assert store.coverage("tenant-a")["SKU-1"]["remaining_units"] == 501 * 20


def test_invalid_eta_is_rejected_and_late_events_do_not_overwrite_newer_state() -> None:
    store = InMemoryOpenOrderStore()

    assert store.observe_event(_shipment("tenant-a", eta="tomorrow")) is None
    current = store.observe_event(
        _shipment("tenant-a", received_units=5, ts="2026-07-13T12:00:00Z")
    )
    stale = store.observe_event(
        _shipment("tenant-a", received_units=0, ts="2026-07-13T11:00:00Z")
    )

    assert current is not None
    assert stale == current
    assert store.coverage("tenant-a")["SKU-1"]["remaining_units"] == 15
