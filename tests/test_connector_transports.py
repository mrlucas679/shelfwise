from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from shelfwise_connectors import (
    InboundRecord,
    InMemoryCursorStore,
    InMemoryWebhookDedupStore,
    PollingConnector,
    SourceSystem,
    ValidationResult,
    WebhookReceiver,
    verify_signature,
)


def _record(object_id: str, raw: dict[str, object] | None = None) -> InboundRecord:
    return InboundRecord(
        tenant_id="tenant_1",
        source_system=SourceSystem.SQUARE,
        source_object_type="inventory_state",
        source_object_id=object_id,
        event_time=datetime(2026, 7, 6, 10, 14, tzinfo=UTC),
        raw_payload=raw or {"id": object_id},
        canonical_type="inventory_state",
        canonical_payload={"sku": object_id, "location_id": "store_12", "quantity": "1"},
        correlation_id=object_id,
        validation=ValidationResult(),
    )


class FakePollConnector(PollingConnector):
    source_system = SourceSystem.SQUARE

    def __init__(self, cursors: InMemoryCursorStore) -> None:
        super().__init__(cursors, tenant_id="tenant_1")
        self.seen_cursors: list[str | None] = []
        self.pages = [
            ([_record("1"), _record("2")], "cursor_2"),
            ([_record("2"), _record("3")], None),
        ]

    async def fetch_page(
        self,
        cursor: str | None,
    ) -> tuple[list[InboundRecord], str | None]:
        self.seen_cursors.append(cursor)
        return self.pages.pop(0)


def test_polling_connector_dedupes_overlap_and_advances_cursor() -> None:
    async def run() -> tuple[list[str], str | None, list[str | None]]:
        cursors = InMemoryCursorStore()
        connector = FakePollConnector(cursors)
        ids = [record.source_object_id async for record in connector.pull()]
        cursor = await cursors.get(tenant_id="tenant_1", system=SourceSystem.SQUARE)
        return ids, cursor, connector.seen_cursors

    ids, cursor, seen_cursors = asyncio.run(run())

    assert ids == ["1", "2", "3"]
    assert cursor == "cursor_2"
    assert seen_cursors == [None, "cursor_2"]


def test_webhook_receiver_verifies_hmac_and_dedupes_event_delivery() -> None:
    async def run() -> tuple[InboundRecord | None, InboundRecord | None]:
        body = b'{"id":"evt_1"}'
        receiver = WebhookReceiver(
            secret="secret",
            dedup=InMemoryWebhookDedupStore(),
            build=lambda _payload: _record("evt_1"),
        )
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        first = await receiver.receive(
            signature=signature,
            body=body,
            event_id="evt_1",
            payload={"id": "evt_1"},
        )
        duplicate = await receiver.receive(
            signature=signature,
            body=body,
            event_id="evt_1",
            payload={"id": "evt_1"},
        )
        return first, duplicate

    first, duplicate = asyncio.run(run())

    assert first is not None
    assert duplicate is None


def test_verify_signature_accepts_hex_and_base64_sha256_hmac() -> None:
    body = b'{"id":"evt_1"}'
    digest = hmac.new(b"secret", body, hashlib.sha256).digest()

    assert verify_signature("secret", body, digest.hex()) is True
    assert verify_signature("secret", body, f"sha256={digest.hex()}") is True
    assert verify_signature("secret", body, base64.b64encode(digest).decode("ascii")) is True
    assert verify_signature("secret", body, "bad") is False


def test_webhook_receiver_rejects_bad_signature() -> None:
    async def run() -> None:
        receiver = WebhookReceiver(
            secret="secret",
            dedup=InMemoryWebhookDedupStore(),
            build=lambda _payload: _record("evt_1"),
        )
        with pytest.raises(PermissionError, match="invalid webhook signature"):
            await receiver.receive(
                signature="bad",
                body=b"{}",
                event_id="evt_1",
                payload={},
            )

    asyncio.run(run())
