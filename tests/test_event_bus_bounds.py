from __future__ import annotations

import json

from shelfwise_backend.event_bus import (
    DEFAULT_EVENT_STREAM_MAXLEN,
    MAX_EVENT_STREAM_MAXLEN,
    MIN_EVENT_STREAM_MAXLEN,
    InMemoryEventBus,
    RedisStreamsEventBus,
    event_stream_maxlen,
    stream_name,
)
from shelfwise_contracts import Event


class FakeRedis:
    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict, dict]] = []
        self.xautoclaim_calls: list[tuple[str, str, str, int]] = []
        self.group_calls: list[tuple[str, str]] = []
        self.ack_calls: list[tuple[str, str, str]] = []
        self.pending_error: Exception | None = None

    def xadd(self, stream: str, fields: dict, **kwargs: object) -> bytes:
        self.xadd_calls.append((stream, fields, kwargs))
        return b"1-0"

    def scan_iter(self, *, match: str) -> list[bytes]:
        assert match == "shelfwise:events:*"
        return [
            b"shelfwise:events:tenant_a",
            b"shelfwise:events:tenant_a:dead",
            b"shelfwise:events:tenant_b",
        ]

    def xgroup_create(self, stream: str, group: str, **_: object) -> None:
        self.group_calls.append((stream, group))

    def xautoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        **_: object,
    ) -> tuple[str, list[tuple[str, dict]], list[str]]:
        self.xautoclaim_calls.append((stream, group, consumer, min_idle_ms))
        return "0-0", [("1-0", {})], []

    def xrange(self, stream: str, **_: object) -> list[tuple[bytes, dict[bytes, bytes]]]:
        return [(b"1-0", {b"event": json.dumps(_event().to_dict()).encode()})]

    def xack(self, stream: str, group: str, message_id: str) -> None:
        self.ack_calls.append((stream, group, message_id))

    def xlen(self, _stream: str) -> int:
        return 0

    def xpending(self, _stream: str, _group: str) -> dict[str, int]:
        if self.pending_error is not None:
            raise self.pending_error
        return {"pending": 0}


def _event() -> Event:
    return Event.parse_wire(
        {
            "id": "evt_bounds",
            "type": "scan",
            "ts": "2026-07-13T10:14:00Z",
            "actor": "store_12",
            "source": "scanner",
            "tenant_id": "tenant_a",
            "payload": {"sku": "4011", "location": "store_12"},
        }
    )


def _redis_bus(client: FakeRedis, *, maxlen: int = 1234) -> RedisStreamsEventBus:
    bus = RedisStreamsEventBus.__new__(RedisStreamsEventBus)
    bus._client = client
    bus._stream_maxlen = maxlen
    return bus


def test_event_stream_maxlen_is_configurable_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_EVENT_STREAM_MAXLEN", "1234")
    assert event_stream_maxlen() == 1234
    assert event_stream_maxlen("0") == MIN_EVENT_STREAM_MAXLEN
    assert event_stream_maxlen("999999999") == MAX_EVENT_STREAM_MAXLEN
    assert event_stream_maxlen("invalid") == DEFAULT_EVENT_STREAM_MAXLEN


def test_redis_xadd_bounds_source_and_dead_letter_streams() -> None:
    client = FakeRedis()
    bus = _redis_bus(client)

    assert bus.publish(_event()) == "1-0"
    bus._move_to_dead_letter(stream_name("tenant_a"), "cascade", "1-0")

    assert [call[0] for call in client.xadd_calls] == [
        "shelfwise:events:tenant_a",
        "shelfwise:events:tenant_a:dead",
    ]
    assert all(call[2] == {"maxlen": 1234, "approximate": True} for call in client.xadd_calls)
    assert client.ack_calls == [("shelfwise:events:tenant_a", "cascade", "1-0")]


def test_redis_reclaim_without_stream_covers_source_streams_only() -> None:
    client = FakeRedis()
    bus = _redis_bus(client)

    reclaimed = bus.reclaim_stale(group="cascade", consumer="worker-2", min_idle_ms=9000)

    assert reclaimed == 2
    assert client.group_calls == [
        ("shelfwise:events:tenant_a", "cascade"),
        ("shelfwise:events:tenant_b", "cascade"),
    ]
    assert client.xautoclaim_calls == [
        ("shelfwise:events:tenant_a", "cascade", "worker-2", 9000),
        ("shelfwise:events:tenant_b", "cascade", "worker-2", 9000),
    ]


def test_redis_stats_treats_missing_consumer_group_as_empty() -> None:
    client = FakeRedis()
    client.pending_error = RuntimeError("NOGROUP No such key or consumer group")
    bus = _redis_bus(client)

    assert bus.stats(tenant_id="tenant_a") == {
        "backend": "redis",
        "messages_total": 0,
        "pending_messages": 0,
        "acked_messages": None,
        "streams": ["shelfwise:events:tenant_a"],
    }


def test_in_memory_reclaim_is_a_no_op() -> None:
    assert InMemoryEventBus().reclaim_stale() == 0
