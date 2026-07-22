"""Client CSV intake: the productized onboarding path for shops without a live connector.

Covers the two-step preview/commit contract end to end: messy-header mapping inference,
per-row validation (bad rows quarantine, they never abort the file), idempotent
re-commit, catalog population from a products file, and the expiry seam that turns a
dated stock export into EXPIRY_ENTRY pipeline events.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_connectors import build_records, preview_csv, record_to_event
from shelfwise_connectors.csv_intake import CsvIntakeError, infer_mapping

TENANT = "local"


# ---------------------------------------------------------------------------
# Mapping inference
# ---------------------------------------------------------------------------


def test_infer_mapping_matches_messy_real_world_headers() -> None:
    headers = ["Item Code", "Product Name", "Qty On Hand", "Branch", "Best Before"]

    mapping, missing = infer_mapping("expiry", headers)

    assert mapping == {
        "sku": "Item Code",
        "location_id": "Branch",
        "quantity": "Qty On Hand",
        "expiry_date": "Best Before",
    }
    assert missing == ()


def test_infer_mapping_reports_missing_required_columns() -> None:
    mapping, missing = infer_mapping("sales", ["SKU", "Qty"])

    assert mapping["sku"] == "SKU"
    assert set(missing) == {"unit_price", "sold_at"}


def test_mapping_override_wins_and_unknown_override_is_rejected() -> None:
    headers = ["code", "how_many", "shop"]

    mapping, missing = infer_mapping(
        "stock", headers, {"sku": "code", "quantity": "how_many", "location_id": "shop"}
    )
    assert mapping["quantity"] == "how_many"
    assert missing == ()

    try:
        infer_mapping("stock", headers, {"not_a_field": "code"})
    except CsvIntakeError as exc:
        assert "unknown mapping field" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown override field must be rejected")


# ---------------------------------------------------------------------------
# Preview (dry run)
# ---------------------------------------------------------------------------


def test_preview_counts_valid_and_invalid_rows_without_aborting() -> None:
    text = (
        "sku,location,quantity,best before\n"
        "yog_1,store_12,10,2026-08-01\n"
        "yog_2,store_12,not_a_number,2026-08-01\n"
        "yog_3,store_12,5,01/08/2026\n"
    )

    preview = preview_csv("expiry", text, tenant_id=TENANT)

    assert preview.row_count == 3
    assert preview.valid_rows == 1
    assert preview.invalid_rows == 2
    issues_by_row = {issue.row: issue.errors for issue in preview.issues}
    assert any("not numeric" in error for error in issues_by_row[3])
    assert any("ISO date" in error for error in issues_by_row[4])
    assert preview.sample[0]["sku"] == "yog_1"


def test_preview_with_unmapped_required_columns_validates_nothing() -> None:
    preview = preview_csv("stock", "foo,bar\n1,2\n", tenant_id=TENANT)

    assert set(preview.missing_required) == {"sku", "location_id", "quantity"}
    assert preview.valid_rows == 0
    assert preview.file_warnings


def test_preview_rejects_empty_and_duplicate_header_files() -> None:
    for text, fragment in (
        ("", "empty"),
        ("sku,sku\n1,2\n", "duplicate column"),
    ):
        try:
            preview_csv("stock", text, tenant_id=TENANT)
        except CsvIntakeError as exc:
            assert fragment in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"expected CsvIntakeError for {text!r}")


# ---------------------------------------------------------------------------
# Record building and the expiry event seam
# ---------------------------------------------------------------------------


def test_build_records_are_idempotent_by_content() -> None:
    text = "sku,location,quantity\nyog_1,store_12,10\n"

    first = build_records("stock", text, tenant_id=TENANT)
    second = build_records("stock", text, tenant_id=TENANT)

    assert len(first) == 1
    assert first[0].source_object_id == "yog_1:store_12"
    assert first[0].payload_hash == second[0].payload_hash


def test_invalid_row_becomes_failed_validation_record_not_an_exception() -> None:
    text = "sku,location,quantity\n,store_12,10\n"

    records = build_records("stock", text, tenant_id=TENANT)

    assert len(records) == 1
    assert not records[0].validation.ok
    assert record_to_event(records[0]) is None


def test_expiry_record_maps_to_expiry_entry_event() -> None:
    text = "sku,location,quantity,expiry date\nyog_1,store_12,10,2026-08-01\n"

    record = build_records("expiry", text, tenant_id=TENANT)[0]
    event = record_to_event(record)

    assert event is not None
    assert event.type.value == "expiry_entry"
    assert event.payload["sku"] == "yog_1"
    assert event.payload["location"] == "store_12"
    assert event.payload["on_hand"] == 10
    assert event.payload["expiry_date"] == "2026-08-01"


def test_sales_rows_without_order_id_get_content_derived_identity_and_warning() -> None:
    text = (
        "sku,qty,price,date\n"
        "yog_1,2,19.99,2026-07-20T10:00:00\n"
        "yog_1,1,19.99,2026-07-20T11:00:00\n"
    )

    records = build_records(
        "sales",
        text,
        tenant_id=TENANT,
        mapping={"quantity": "qty", "unit_price": "price", "sold_at": "date"},
    )

    assert len(records) == 2
    assert records[0].source_object_id != records[1].source_object_id
    assert records[0].validation.ok
    assert any("order_id" in warning for warning in records[0].validation.warnings)


def test_fractional_sales_quantity_is_a_row_error() -> None:
    text = "sku,quantity,unit price,sold at\nyog_1,1.5,19.99,2026-07-20\n"

    record = build_records("sales", text, tenant_id=TENANT)[0]

    assert not record.validation.ok
    assert any("fractional" in error for error in record.validation.errors)


# ---------------------------------------------------------------------------
# HTTP flow: preview writes nothing, commit is idempotent, catalog fills
# ---------------------------------------------------------------------------


def test_preview_endpoint_writes_nothing() -> None:
    client = TestClient(app)
    body = {
        "kind": "stock",
        "csv_text": "sku,location,quantity\ncsvint_prev,store_12,10\n",
    }

    response = client.post("/intake/csv/preview", json=body)
    records = client.get("/connectors/inbound-records")

    assert response.status_code == 200
    assert response.json()["valid_rows"] == 1
    assert all(
        row["source_object_id"] != "csvint_prev:store_12"
        for row in records.json()["records"]
    )


def test_commit_stock_csv_pipelines_events_and_recommit_dedups() -> None:
    client = TestClient(app)
    body = {
        "kind": "stock",
        "csv_text": (
            "sku,location,quantity\n"
            "csvint_a,store_12,10\n"
            "csvint_b,store_12,0\n"
        ),
    }

    first = client.post("/intake/csv/commit", json=body)
    duplicate = client.post("/intake/csv/commit", json=body)

    assert first.status_code == 200
    outcome = first.json()
    assert outcome["rows"] == 2
    assert set(outcome["summary"]) == {"accepted"}
    event = outcome["records"][0]["event"]
    assert event["type"] == "stock_update"
    assert event["payload"]["sku"] == "csvint_a"

    assert duplicate.status_code == 200
    assert duplicate.json()["summary"] == {"duplicate": 2}


def test_commit_mixed_validity_quarantines_bad_rows_and_ingests_good_ones() -> None:
    client = TestClient(app)
    body = {
        "kind": "stock",
        "csv_text": (
            "sku,location,quantity\n"
            "csvint_ok,store_12,10\n"
            ",store_12,10\n"
        ),
    }

    response = client.post("/intake/csv/commit", json=body)

    assert response.status_code == 200
    assert response.json()["summary"] == {"accepted": 1, "invalid": 1}


def test_commit_products_csv_populates_catalog_and_flags_conflicts() -> None:
    client = TestClient(app)
    body = {
        "kind": "products",
        "csv_text": (
            "item code,product name,category,brand,pack size,uom,barcode\n"
            "csvint_p1,Full Cream Milk 1L,dairy,DairyCo,1L,each,6001000000017\n"
            "csvint_p1,Low Fat Milk 1L,dairy,DairyCo,1L,each,6001000000024\n"
        ),
    }

    response = client.post("/intake/csv/commit", json=body)

    assert response.status_code == 200
    outcome = response.json()
    statuses = [row["status"] for row in outcome["records"]]
    assert statuses[0] == "cataloged"
    # Same item code on a different product name is exactly the conflict a human must
    # review, not a silent remap.
    assert statuses[1] == "identifier_conflict"

    resolved = client.get("/catalog/resolve", params={"kind": "sku", "value": "csvint_p1"})
    assert resolved.status_code == 200
    assert resolved.json()["variant"]["variant_id"].startswith("var_")


def test_commit_expiry_csv_reaches_the_event_bus() -> None:
    client = TestClient(app)
    body = {
        "kind": "expiry",
        "csv_text": "sku,location,quantity,expiry date\ncsvint_e1,store_12,10,2026-08-01\n",
    }

    response = client.post("/intake/csv/commit", json=body)

    assert response.status_code == 200
    record = response.json()["records"][0]
    assert record["event"]["type"] == "expiry_entry"
    assert record["event"]["payload"]["expiry_date"] == "2026-08-01"


def test_commit_rejects_files_with_unmapped_required_columns() -> None:
    client = TestClient(app)
    body = {"kind": "stock", "csv_text": "foo,bar\n1,2\n"}

    response = client.post("/intake/csv/commit", json=body)

    assert response.status_code == 422
    assert "required columns are unmapped" in response.json()["detail"]
