from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from threading import Lock
from typing import Any

from ..gateway import MAX_WEBHOOK_BYTES
from ..provenance import InboundRecord

RecordOrRecords = InboundRecord | list[InboundRecord]
BuildRecord = Callable[[dict[str, Any]], RecordOrRecords | Awaitable[RecordOrRecords]]


class InMemoryWebhookDedupStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._seen: set[str] = set()

    async def seen(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._seen

    async def mark(self, event_id: str) -> None:
        with self._lock:
            self._seen.add(event_id)

    async def mark_if_new(self, event_id: str) -> bool:
        """Atomically check-and-mark. Returns True the first time an id is seen.

        A separate seen()-then-mark() pair leaves a window where two concurrent
        deliveries of the same at-least-once webhook retry can both pass the check
        before either marks it, defeating dedup - this collapses both steps into one
        lock acquisition.
        """
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            return True

    async def unmark(self, event_id: str) -> None:
        """Release a failed delivery so the provider's at-least-once retry can run."""
        with self._lock:
            self._seen.discard(event_id)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


class WebhookReceiver:
    def __init__(
        self,
        *,
        secret: str,
        dedup: InMemoryWebhookDedupStore,
        build: BuildRecord,
    ) -> None:
        self._secret = secret
        self._dedup = dedup
        self._build = build

    async def receive(
        self,
        *,
        signature: str,
        body: bytes,
        event_id: str,
        payload: dict[str, Any],
    ) -> RecordOrRecords | None:
        if len(body) > MAX_WEBHOOK_BYTES:
            raise ValueError("webhook body exceeds size cap")
        if not verify_signature(self._secret, body, signature):
            raise PermissionError("invalid webhook signature")
        if not await self._dedup.mark_if_new(event_id):
            return None
        try:
            result = self._build(payload)
            if isawaitable(result):
                return await result
            return result
        except BaseException:
            # Do not turn a temporary mapper/database failure into permanent data loss.
            # The first caller still receives the error, while the source's retry can claim
            # and process this delivery again.
            await self._dedup.unmark(event_id)
            raise


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    supplied = _clean_signature(signature)
    hex_expected = expected.hex()
    b64_expected = base64.b64encode(expected).decode("ascii")
    return hmac.compare_digest(hex_expected, supplied) or hmac.compare_digest(
        b64_expected,
        supplied,
    )


def _clean_signature(signature: str) -> str:
    value = signature.strip()
    if value.lower().startswith("sha256="):
        return value.split("=", 1)[1].strip()
    return value
