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
