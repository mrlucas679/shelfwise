from __future__ import annotations

import json
import os
from collections import deque
from contextlib import suppress
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_contracts import Event


class InMemoryEventBus:
    def __init__(self, *, max_retries: int = 3) -> None:
        self._lock = Lock()
        self._max_retries = max(1, max_retries)
        self._messages: list[dict[str, Any]] = []
        self._pending: deque[dict[str, Any]] = deque()
        self._acked: list[str] = []
        self._dead_letter: list[dict[str, Any]] = []
        self._delivery_counts: dict[str, int] = {}

    def publish(self, event: Event) -> str:
        with self._lock:
            message_id = f"mem-{len(self._messages) + 1}"
            message = {
                "message_id": message_id,
                "stream": stream_name(event.tenant_id),
                "event": event.to_dict(),
            }
            self._messages.append(message)
            self._pending.append(message)
            self._delivery_counts[message_id] = 0
            return message_id

    def consume_one(self, stream: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            found: dict[str, Any] | None = None
            if stream is None:
                found = self._pending.popleft() if self._pending else None
            else:
                for index, message in enumerate(self._pending):
                    if message["stream"] == stream:
                        found = self._pending[index]
                        del self._pending[index]
                        break
            if found is None:
                return None
            message_id = str(found["message_id"])
            self._delivery_counts[message_id] = self._delivery_counts.get(message_id, 0) + 1
            return deepcopy(found)

    def ack(self, stream: str, message_id: str) -> None:
        with self._lock:
            self._acked.append(f"{stream}:{message_id}")
            self._delivery_counts.pop(message_id, None)

    def nack(self, stream: str, message_id: str, *, group: str | None = None) -> bool:
        """Return the message for retry, or dead-letter it past max_retries.

        Returns True when the message was moved to the dead-letter queue (no further
        redelivery), False when it was requeued for another attempt.
        """
        del group
        with self._lock:
            source = next(
                (item for item in self._messages if item["message_id"] == message_id),
                None,
            )
            if source is None:
                return False
            delivered = self._delivery_counts.get(message_id, 1)
            if delivered >= self._max_retries:
                self._dead_letter.append(deepcopy(source))
                self._delivery_counts.pop(message_id, None)
                return True
            self._pending.append(deepcopy(source))
            return False

    def dead_letter(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item) for item in self._dead_letter]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item) for item in reversed(self._messages)]

    def stats(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        stream = stream_name(tenant_id) if tenant_id else None
        with self._lock:
            messages = [
                item for item in self._messages if stream is None or item["stream"] == stream
            ]
            pending = [
                item for item in self._pending if stream is None or item["stream"] == stream
            ]
            acked = [
                item
                for item in self._acked
                if stream is None or str(item).startswith(f"{stream}:")
            ]
            dead_letter = [
                item for item in self._dead_letter if stream is None or item["stream"] == stream
            ]
        return {
            "backend": "memory",
            "messages_total": len(messages),
            "pending_messages": len(pending),
            "acked_messages": len(acked),
            "dead_letter_messages": len(dead_letter),
            "streams": sorted({str(item["stream"]) for item in messages}),
        }

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._pending.clear()
            self._acked.clear()
            self._dead_letter.clear()
            self._delivery_counts.clear()


class RedisStreamsEventBus:
    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise ValueError("REDIS_URL is required for RedisStreamsEventBus")
        self._redis_url = redis_url
        self._client = self._connect()

    def publish(self, event: Event) -> str:
        message_id = self._client.xadd(
            stream_name(event.tenant_id),
            {"event": json.dumps(event.to_dict(), separators=(",", ":"))},
        )
        return _text(message_id)

    def consume_one(
        self,
        stream: str | None = None,
        *,
        group: str = "cascade",
        consumer: str = "worker-1",
    ) -> dict[str, Any] | None:
        streams = (
            [stream]
            if stream
            else [_text(key) for key in self._client.scan_iter("shelfwise:events:*")]
        )
        for item in streams:
            self._ensure_group(item, group)
            batches = self._client.xreadgroup(group, consumer, {item: ">"}, count=1, block=1)
            for stream_name_value, messages in batches or []:
                for message_id, fields in messages:
                    raw = fields.get(b"event") or fields.get("event")
                    return {
                        "message_id": _text(message_id),
                        "stream": _text(stream_name_value),
                        "event": json.loads(_text(raw)),
                    }
        return None

    def ack(self, stream: str, message_id: str, *, group: str = "cascade") -> None:
        self._client.xack(stream, group, message_id)

    def nack(
        self,
        stream: str,
        message_id: str,
        *,
        group: str = "cascade",
        max_retries: int = 3,
    ) -> bool:
        """Leave the message pending for redelivery, or dead-letter it past max_retries.

        Redis already keeps a nacked (non-acked) message in the consumer group's pending
        entries list, so "requeue" here is a no-op beyond checking delivery count. Returns
        True when the message was moved to the dead-letter stream and acked off the source
        stream (no further redelivery), False when it stays pending for retry.
        """
        delivered = self._delivery_count(stream, group, message_id)
        if delivered < max(1, max_retries):
            return False
        self._move_to_dead_letter(stream, group, message_id)
        return True

    def reclaim_stale(
        self,
        stream: str,
        *,
        group: str = "cascade",
        consumer: str = "worker-1",
        min_idle_ms: int = 30_000,
    ) -> int:
        """Reclaim pending messages idle longer than min_idle_ms (crash recovery)."""
        try:
            self._ensure_group(stream, group)
            _cursor, claimed, _deleted = self._client.xautoclaim(
                stream, group, consumer, min_idle_ms, start_id="0-0", count=50
            )
        except Exception:
            return 0
        return len(claimed)

    def _delivery_count(self, stream: str, group: str, message_id: str) -> int:
        try:
            entries = self._client.xpending_range(
                stream, group, min=message_id, max=message_id, count=1
            )
        except Exception:
            return 1
        if not entries:
            return 1
        entry = entries[0]
        raw_count = entry.get("times_delivered", entry.get(b"times_delivered", 1))
        return int(raw_count)

    def _move_to_dead_letter(self, stream: str, group: str, message_id: str) -> None:
        try:
            rows = self._client.xrange(stream, min=message_id, max=message_id, count=1)
        except Exception:
            rows = []
        fields = dict(rows[0][1]) if rows else {}
        fields["dead_lettered_from"] = message_id
        self._client.xadd(f"{stream}:dead", fields)
        with suppress(Exception):
            self._client.xack(stream, group, message_id)

    def dead_letter(self, stream: str, *, count: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for message_id, fields in self._client.xrevrange(f"{stream}:dead", count=count):
            raw = fields.get(b"event") or fields.get("event")
            rows.append(
                {
                    "message_id": _text(message_id),
                    "stream": stream,
                    "event": json.loads(_text(raw)) if raw else {},
                }
            )
        return rows

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in self._client.scan_iter(match="shelfwise:events:*"):
            for message_id, fields in self._client.xrevrange(key, count=200):
                raw = fields.get(b"event") or fields.get("event")
                rows.append(
                    {
                        "message_id": _text(message_id),
                        "stream": _text(key),
                        "event": json.loads(_text(raw)),
                    }
                )
        return rows

    def stats(self, *, tenant_id: str | None = None, group: str = "cascade") -> dict[str, Any]:
        streams = [stream_name(tenant_id)] if tenant_id else [
            _text(key) for key in self._client.scan_iter(match="shelfwise:events:*")
        ]
        messages_total = 0
        pending_messages = 0
        for stream in streams:
            messages_total += int(self._client.xlen(stream))
            pending_messages += _pending_count(self._client, stream, group)
        return {
            "backend": "redis",
            "messages_total": messages_total,
            "pending_messages": pending_messages,
            "acked_messages": None,
            "streams": sorted(streams),
        }

    def clear(self) -> None:
        keys = list(self._client.scan_iter(match="shelfwise:events:*"))
        if keys:
            self._client.delete(*keys)

    def _ensure_group(self, stream: str, group: str) -> None:
        try:
            self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _connect(self) -> Any:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Install redis to use Redis Streams") from exc
        return redis.Redis.from_url(self._redis_url)


def create_event_bus() -> InMemoryEventBus | RedisStreamsEventBus:
    backend = os.getenv("SHELFWISE_BUS_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryEventBus()
    if backend == "redis":
        return RedisStreamsEventBus(os.getenv("REDIS_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_BUS_BACKEND: {backend}")


def stream_name(tenant_id: str) -> str:
    tenant = tenant_id.strip() or "default"
    return f"shelfwise:events:{tenant}"


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _pending_count(client: Any, stream: str, group: str) -> int:
    try:
        pending = client.xpending(stream, group)
    except Exception:
        return 0
    if isinstance(pending, dict):
        return int(pending.get("pending") or pending.get("count") or 0)
    if isinstance(pending, list | tuple) and pending:
        return int(pending[0])
    return 0
