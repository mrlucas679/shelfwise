from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from shelfwise_connectors import (
    MAX_WEBHOOK_BYTES,
    IdentityMap,
    InboundRecord,
    InventoryState,
    SourceSystem,
    TaskWriteBackSink,
    create_inbound_record_store,
    inventory_to_event,
    neutralise_formula,
    parse_gs1,
    quarantine_intake,
    quarantine_webhook_body,
    raw_payload_hash,
    validate_inventory,
)


def _record() -> InboundRecord:
    return InboundRecord(
        tenant_id="tenant_1",
        source_system=SourceSystem.SAP,
        source_object_type="inventory_state",
        source_object_id="stock_4011_store_12",
        event_time=datetime(2026, 7, 6, 10, 14, tzinfo=UTC),
        raw_payload={"Material": "4011", "Qty": "240"},
        canonical_type="inventory_state",
        correlation_id="cor_1",
    )


def test_raw_payload_hash_is_stable_for_key_order() -> None:
    left = raw_payload_hash({"sku": "4011", "qty": 240})
    right = raw_payload_hash({"qty": 240, "sku": "4011"})

    assert left == right
    assert len(left) == 64


def test_identity_map_resolves_by_gtin_barcode_sku_and_source_id() -> None:
    identity = IdentityMap()
    identity.link(
        "prod_4011",
        sku="4011",
        gtin="06001234567890",
        barcode="600123",
        source_id="sap_4011",
    )

    assert identity.resolve(gtin="06001234567890") == "prod_4011"
    assert identity.resolve(barcode="600123") == "prod_4011"
    assert identity.resolve(sku="4011") == "prod_4011"
    assert identity.resolve(source_id="sap_4011") == "prod_4011"
    assert identity.resolve(sku="missing") is None


def test_parse_gs1_extracts_common_application_identifiers() -> None:
    parsed = parse_gs1("(01)06001234567890(17)260707(10)LOT-A")

    assert parsed.gtin == "06001234567890"
    assert parsed.expiry_yymmdd == "260707"
    assert parsed.lot == "LOT-A"


def test_inventory_validation_rejects_negative_or_unresolvable_stock() -> None:
    valid = validate_inventory(
        InventoryState(
            tenant_id="tenant_1",
            sku="4011",
            location_id="store_12",
            quantity=Decimal("240"),
        )
    )
    invalid = validate_inventory(
        InventoryState(
            tenant_id="tenant_1",
            sku="",
            location_id="store_12",
            quantity=Decimal("-1"),
        )
    )

    assert valid.ok is True
    assert invalid.ok is False
    assert "inventory has no resolvable product identifier" in invalid.errors
    assert "inventory quantity cannot be negative" in invalid.errors


def test_inventory_normalizes_to_traceable_stock_update_event() -> None:
    event = inventory_to_event(
        InventoryState(
            tenant_id="tenant_1",
            sku="4011",
            location_id="store_12",
            quantity=Decimal("240"),
        ),
        _record(),
    )

    assert event.id == "evt_sap_inventory_state_stock_4011_store_12"
    assert event.type.value == "stock_update"
    assert event.tenant_id == "tenant_1"
    assert event.correlation_id == "cor_1"
    assert event.payload["sku"] == "4011"
    assert event.payload["raw_payload_hash"] == _record().payload_hash


def test_task_writeback_sink_is_idempotent() -> None:
    sink = TaskWriteBackSink()
    first = sink.create_task(
        idempotency_key="writeback:dec_1",
        tenant_id="tenant_1",
        title="Approve markdown",
        action={"type": "apply_markdown", "sku": "4011"},
        rollback_instructions={"rollback": "cancel_pending_task"},
    )
    second = sink.create_task(
        idempotency_key="writeback:dec_1",
        tenant_id="tenant_1",
        title="Approve markdown again",
        action={"type": "apply_markdown", "sku": "4011"},
    )

    assert first == second
    assert first["status"] == "pending_external_write"
    assert first["rollback_instructions"]["rollback"] == "cancel_pending_task"
    assert sink.list(tenant_id="tenant_1") == [first]
    assert sink.list(tenant_id="tenant_2") == []
    sink.clear()
    assert sink.list(tenant_id="tenant_1") == []


def test_quarantine_rejects_disguised_binaries_and_accepts_clean_csv() -> None:
    exe = quarantine_intake(b"MZ\x90\x00pretend.csv", claimed_mime="text/csv")
    xlsx = quarantine_intake(b"PK\x03\x04fake_sheet", claimed_mime="text/csv")
    nul = quarantine_intake(b"col\x00umn,1\n", claimed_mime="text/csv")
    bad_utf = quarantine_intake(b"\xff\xfe\x00garbage", claimed_mime="text/csv")
    ok = quarantine_intake(b"sku,on_hand\n4011,240\n", claimed_mime="text/csv")

    assert exe.accepted is False
    assert exe.kind == "executable"
    assert len(exe.sha256) == 64
    assert xlsx.accepted is False
    assert xlsx.kind == "zip"
    assert nul.accepted is False
    assert bad_utf.accepted is False
    assert ok.accepted is True
    assert ok.text is not None
    assert "4011,240" in ok.text


def test_quarantine_neutralises_spreadsheet_formula_injection() -> None:
    verdict = quarantine_intake(
        b'sku,note\n4011,=HYPERLINK("http://evil")\n9001,+SUM(1)\n',
        claimed_mime="text/csv",
    )

    assert verdict.accepted is True
    assert verdict.text is not None
    assert "'=HYPERLINK" in verdict.text
    assert "'+SUM" in verdict.text
    assert neutralise_formula("@cmd") == "'@cmd"
    assert neutralise_formula("plain") == "plain"


def test_quarantine_caps_webhook_bodies() -> None:
    ok = quarantine_webhook_body(b'{"event_id":"e1"}')
    too_large = quarantine_webhook_body(b"0" * (MAX_WEBHOOK_BYTES + 1))

    assert ok.accepted is True
    assert ok.kind == "webhook_body"
    assert too_large.accepted is False
    assert too_large.kind == "too_large"


def test_inbound_records_from_one_payload_get_distinct_stored_ids() -> None:
    """Two line items on one webhook share a raw_payload (and thus payload_hash), but are
    distinct source objects. Their stored `id` (a Postgres primary key) must not collide."""
    store = create_inbound_record_store()
    shared_payload = {"order_id": "ord_1", "lines": [{"sku": "4011"}, {"sku": "4012"}]}
    first = InboundRecord(
        tenant_id="tenant_1",
        source_system=SourceSystem.SHOPIFY,
        source_object_type="sale",
        source_object_id="ord_1:line_1",
        event_time=datetime(2026, 7, 6, 10, 14, tzinfo=UTC),
        raw_payload=shared_payload,
        canonical_type="sale",
        correlation_id="cor_1",
    )
    second = InboundRecord(
        tenant_id="tenant_1",
        source_system=SourceSystem.SHOPIFY,
        source_object_type="sale",
        source_object_id="ord_1:line_2",
        event_time=datetime(2026, 7, 6, 10, 14, tzinfo=UTC),
        raw_payload=shared_payload,
        canonical_type="sale",
        correlation_id="cor_1",
    )

    _, stored_first = store.record(first)
    _, stored_second = store.record(second)

    assert stored_first["id"] != stored_second["id"]
