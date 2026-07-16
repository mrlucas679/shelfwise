"""Redis Streams contract tests for the production event bus.

Gated on SHELFWISE_TEST_REDIS_URL exactly like the Postgres schema-contract file is
gated on SHELFWISE_TEST_DATABASE_URL: everywhere else the suite exercises the bus only
through InMemoryEventBus or hand-rolled fakes, which cannot prove the real Redis
semantics this bus depends on - `times_delivered` incrementing on pending-history
redelivery (the dead-letter budget), XAUTOCLAIM reclaim of a dead consumer's messages,
approximate MAXLEN trimming, and consumer-group isolation. First proven live 2026-07-15
(all passed); this file keeps that proof repeatable in CI.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from shelfwise_backend.event_bus import RedisStreamsEventBus, stream_name
from shelfwise_contracts import DataDomain, Event, EventType

_REDIS_URL = os.getenv("SHELFWISE_TEST_REDIS_URL", "")
_TENANT = "redis_bus_contract"

pytestmark = pytest.mark.skipif(
    not _REDIS_URL,
    reason="SHELFWISE_TEST_REDIS_URL not set - live Redis bus contract test skipped",
)


def _event(index: int) -> Event:
    return Event(
        id=f"evt_redis_contract_{index}",
        type=EventType.SCAN,
        ts=datetime.now(UTC),
        actor="contract",
        tenant_id=_TENANT,
        data_domain=DataDomain.WORLD_SIMULATION,
        correlation_id=f"redis_contract_{index}",
        payload={"sku": f"SKU-{index}", "n": index},
    )


@pytest.fixture()
def bus() -> RedisStreamsEventBus:
    instance = RedisStreamsEventBus(_REDIS_URL)
    instance.clear()
    yield instance
    instance.clear()


def test_publish_consume_ack_roundtrip(bus: RedisStreamsEventBus) -> None:
    stream = stream_name(_TENANT)
    message_id = bus.publish(_event(1))

    message = bus.consume_one(stream)
    assert message is not None
    assert message["message_id"] == message_id
    assert message["event"]["id"] == "evt_redis_contract_1"

    bus.ack(stream, message["message_id"])
    assert bus.consume_one(stream) is None


def test_nack_redelivers_then_dead_letters_past_max_retries(bus: RedisStreamsEventBus) -> None:
    stream = stream_name(_TENANT)
    message_id = bus.publish(_event(2))
    message = bus.consume_one(stream)
    assert message is not None and message["message_id"] == message_id

    assert bus.nack(stream, message_id, max_retries=3) is False, (
        "first failure must requeue, not dead-letter"
    )
    redelivered = bus.consume_one(stream)
    assert redelivered is not None and redelivered["message_id"] == message_id, (
        "a nacked message must be redelivered to the same consumer via pending history"
    )

    # times_delivered increments on each history redelivery until the budget is spent.
    attempts = 1
    dead = False
    while not dead and attempts < 10:
        dead = bus.nack(stream, message_id, max_retries=3)
        if not dead:
            assert bus.consume_one(stream) is not None
        attempts += 1
    assert dead, "delivery counter must eventually exhaust the retry budget"

    dead_rows = bus.dead_letter(stream)
    assert any(row["event"].get("id") == "evt_redis_contract_2" for row in dead_rows)
    assert bus.consume_one(stream) is None, "a dead-lettered message must never redeliver"


def test_reclaim_stale_recovers_a_dead_consumers_pending_message(
    bus: RedisStreamsEventBus,
) -> None:
    stream = stream_name(_TENANT)
    message_id = bus.publish(_event(3))

    taken = bus.consume_one(stream, consumer="worker-dead")
    assert taken is not None and taken["message_id"] == message_id
    assert bus.consume_one(stream) is None, (
        "another consumer must not see a message pending in a different consumer's PEL"
    )

    assert bus.reclaim_stale(stream, min_idle_ms=0) >= 1
    reclaimed = bus.consume_one(stream)
    assert reclaimed is not None and reclaimed["message_id"] == message_id
    bus.ack(stream, message_id)


def test_stream_is_trimmed_near_configured_maxlen(bus: RedisStreamsEventBus) -> None:
    small = RedisStreamsEventBus(_REDIS_URL, stream_maxlen=100)
    for index in range(100, 350):
        small.publish(_event(index))
    length = int(small._client.xlen(stream_name(_TENANT)))
    assert length < 350, "MAXLEN ~ trimming must bound the stream"


def test_streamless_consume_discovers_streams_and_clear_removes_dead(
    bus: RedisStreamsEventBus,
) -> None:
    bus.publish(_event(4))
    message = bus.consume_one()
    assert message is not None, "consume without a stream must discover tenant streams"

    bus.clear()
    leftovers = list(bus._client.scan_iter(match="shelfwise:events:*"))
    assert leftovers == [], "clear must remove event streams including :dead"
