from __future__ import annotations

from shelfwise_backend.event_store import InMemoryEventStore
from shelfwise_contracts import Event
from shelfwise_runtime.provenance import DataDomain


def _event(tenant_id: str) -> Event:
    return Event.parse_wire(
        {
            "id": "shared-source-id",
            "type": "scan",
            "ts": "2026-07-13T10:00:00Z",
            "actor": "wms",
            "source": "api",
            "tenant_id": tenant_id,
            "payload": {"sku": "SKU-1"},
        }
    )


def test_event_deduplication_is_tenant_scoped() -> None:
    store = InMemoryEventStore()
    first = _event("tenant-a")
    second = _event("tenant-b")

    assert store.record(first) is True
    assert store.record(second) is True
    assert store.record(first) is False
    assert [row["tenant_id"] for row in store.list(tenant_id="tenant-a")] == ["tenant-a"]
    assert [row["tenant_id"] for row in store.list(tenant_id="tenant-b")] == ["tenant-b"]


def test_event_publication_state_is_tenant_scoped() -> None:
    store = InMemoryEventStore()
    event = _event("tenant-a")
    store.record(event)

    store.mark_published(event.id, tenant_id="tenant-a")

    assert store.is_published(event.id, tenant_id="tenant-a") is True
    assert store.is_published(event.id, tenant_id="tenant-b") is False


def test_event_identity_and_publication_are_data_domain_scoped() -> None:
    store = InMemoryEventStore()
    operational = _event("tenant-a")
    simulation = Event.parse_wire(
        {**operational.to_dict(), "data_domain": DataDomain.WORLD_SIMULATION.value}
    )

    assert store.record(operational) is True
    assert store.record(simulation) is True
    assert len(store.list(tenant_id="tenant-a")) == 2
    assert [
        row["data_domain"]
        for row in store.list(
            tenant_id="tenant-a", data_domain=DataDomain.WORLD_SIMULATION
        )
    ] == ["world_simulation"]

    store.mark_published(
        simulation.id,
        tenant_id="tenant-a",
        data_domain=DataDomain.WORLD_SIMULATION,
    )
    assert store.is_published(
        simulation.id,
        tenant_id="tenant-a",
        data_domain=DataDomain.WORLD_SIMULATION,
    )
    assert not store.is_published(simulation.id, tenant_id="tenant-a")
