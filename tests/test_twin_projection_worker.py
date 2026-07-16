from datetime import UTC, datetime

from shelfwise_contracts import Event, EventType
from shelfwise_runtime.provenance import DataDomain
from shelfwise_twin import InMemoryTwinStore, TwinProjectionWorker, TwinService


class EmptyBus:
    def consume_one(self, **kwargs):
        return None


def test_projection_worker_is_idle_without_messages() -> None:
    worker = TwinProjectionWorker(EmptyBus(), TwinService(InMemoryTwinStore()))
    assert worker.run_once().status == "empty"
    assert worker.reclaim(consumer="test") == 0


class OneMessageBus:
    def __init__(self, event: Event) -> None:
        self.message = {
            "message_id": "message-1",
            "stream": "events",
            "event": event.to_dict(),
        }
        self.acked: list[str] = []

    def consume_one(self, **kwargs):
        message, self.message = self.message, None
        return message

    def ack(self, _stream, message_id, **kwargs):
        self.acked.append(message_id)

    def nack(self, *_args, **_kwargs):
        return False


def test_projection_worker_acknowledges_and_skips_simulation() -> None:
    event = Event(
        id="evt_world_worker",
        type=EventType.STOCK_UPDATE,
        ts=datetime(2026, 7, 13, 8, tzinfo=UTC),
        actor="world",
        tenant_id="tenant-a",
        data_domain=DataDomain.WORLD_SIMULATION,
        payload={"store_id": "store-1", "sku": "SKU-1", "on_hand": 99},
    )
    bus = OneMessageBus(event)
    service = TwinService(InMemoryTwinStore())

    result = TwinProjectionWorker(bus, service).run_once()

    assert result.status == "skipped_non_operational"
    assert bus.acked == ["message-1"]
    assert service.store.list_entities("tenant-a") == []


def test_projection_loop_service_refuses_the_memory_bus_instead_of_stealing_messages(
    monkeypatch,
) -> None:
    """Enabling the twin worker against the in-memory bus would compete with the cascade
    worker for the same pending deque (no consumer groups exist there) - it must refuse
    with an honest reason, never silently corrupt the queue."""
    import asyncio

    from shelfwise_twin import TwinProjectionLoopService

    monkeypatch.setenv("TWIN_PROJECTION_WORKER_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_BUS_BACKEND", "memory")
    service = TwinProjectionLoopService(
        TwinProjectionWorker(EmptyBus(), TwinService(InMemoryTwinStore()))
    )

    asyncio.run(service.start())

    status = service.status()
    assert status["running"] is False
    assert "consumer groups" in (status["refused_reason"] or "")


def test_projection_loop_service_stays_off_by_default(monkeypatch) -> None:
    import asyncio

    from shelfwise_twin import TwinProjectionLoopService

    monkeypatch.delenv("TWIN_PROJECTION_WORKER_ENABLED", raising=False)
    service = TwinProjectionLoopService(
        TwinProjectionWorker(EmptyBus(), TwinService(InMemoryTwinStore()))
    )

    asyncio.run(service.start())

    assert service.status()["enabled"] is False
    assert service.status()["running"] is False
