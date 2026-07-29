"""Client CSV intake: turn an arbitrary shop export into canonical inbound records.

This is the productized onboarding path for a shop whose data arrives as spreadsheet
exports rather than through a live POS/ERP connector. It deliberately reuses the exact
seams the nine system connectors use — `InboundRecord`, the validators, and
`record_to_event` — so a CSV row and a webhook payload travel the same pipeline and
inherit the same dedup, provenance, and quarantine guarantees.

Two-step contract:
- `preview_csv` is the dry run: infer the column mapping, validate every row, and
  report issues without writing anything. The operator (or later, the import UI)
  corrects the mapping or the file and re-previews until clean enough.
- `build_records` produces the committed shape: one `InboundRecord` per data row,
  idempotent by content (a re-uploaded file dedups in the inbound store), with invalid
  rows carried as failed-validation records so they quarantine with provenance instead
  of silently disappearing.

Dates must be ISO (`2026-07-21` or a full timestamp). Ambiguous regional formats like
`01/07/2026` are rejected per row rather than guessed: a wrong silent guess corrupts
expiry math, and expiry math is the product.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from .canonical import (
    ExpiryEntry,
    InventoryState,
    ProductMaster,
    SalesLine,
    SourceSystem,
    StockState,
)
from .connectors.systems._common import now_utc, wrap
from .provenance import InboundRecord, ValidationResult
from .validation import (
    validate_expiry,
    validate_inventory,
    validate_product,
    validate_sales,
)

MAX_CSV_ROWS = 20_000
_PREVIEW_ISSUE_LIMIT = 50
_PREVIEW_SAMPLE_LIMIT = 5

CSV_INTAKE_KINDS = ("products", "stock", "expiry", "sales")

# Canonical field -> accepted header spellings (normalised: lowercase, spaces/dashes/
# underscores collapsed). Extend aliases here — never fork the inference logic per kind.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("sku", "item code", "itemcode", "stock code", "stockcode", "product code", "code"),
    "barcode": ("barcode", "ean", "ean13", "upc"),
    "gtin": ("gtin", "gtin13", "gtin14"),
    "name": ("name", "product name", "item name", "description", "product"),
    "category": ("category", "department", "product category"),
    "brand": ("brand", "manufacturer"),
    "pack_size": ("pack size", "packsize", "size", "pack"),
    "unit_of_measure": ("unit of measure", "uom", "unit", "units"),
    "location_id": ("location", "location id", "store", "store id", "branch", "site"),
    "quantity": (
        "quantity",
        "qty",
        "on hand",
        "onhand",
        "qty on hand",
        "quantity on hand",
        "stock",
        "units on hand",
        "soh",
    ),
    "stock_state": ("stock state", "state", "status"),
    "event_time": ("event time", "as of", "asof", "snapshot date", "count date"),
    "expiry_date": (
        "expiry date",
        "expiry",
        "expires",
        "best before",
        "best before date",
        "sell by",
        "sell by date",
        "use by",
        "sled",
    ),
    "unit_price": ("unit price", "price", "selling price", "unit amount", "price each"),
    "sold_at": ("sold at", "date", "datetime", "timestamp", "sale date", "sale time"),
    "order_id": ("order id", "order", "receipt", "receipt no", "transaction id", "txn id"),
    "line_id": ("line id", "line", "line no", "line number"),
}

_KIND_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # kind -> (required fields, optional fields)
    "products": (
        ("name", "sku"),
        ("barcode", "gtin", "category", "brand", "pack_size", "unit_of_measure"),
    ),
    "stock": (
        ("sku", "location_id", "quantity"),
        ("barcode", "gtin", "stock_state", "event_time"),
    ),
    "expiry": (("sku", "location_id", "quantity", "expiry_date"), ()),
    "sales": (
        ("sku", "quantity", "unit_price", "sold_at"),
        ("location_id", "order_id", "line_id"),
    ),
}


class CsvIntakeError(ValueError):
    """A file-level problem that prevents any row from being processed."""


@dataclass(frozen=True, slots=True)
class CsvRowIssue:
    row: int  # 1-based line number in the file (header is line 1)
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"row": self.row, "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class CsvPreview:
    kind: str
    columns: tuple[str, ...]
    mapping: dict[str, str]
    missing_required: tuple[str, ...]
    row_count: int
    valid_rows: int
    invalid_rows: int
    issues: tuple[CsvRowIssue, ...]
    sample: tuple[dict[str, Any], ...]
    file_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "columns": list(self.columns),
            "mapping": dict(self.mapping),
            "missing_required": list(self.missing_required),
            "row_count": self.row_count,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "issues": [issue.to_dict() for issue in self.issues],
            "sample": [dict(row) for row in self.sample],
            "file_warnings": list(self.file_warnings),
        }


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    line: int
    values: dict[str, str]
    canonical: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult = field(default_factory=ValidationResult)


def infer_mapping(
    kind: str,
    headers: list[str],
    overrides: dict[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Match canonical fields to file headers; overrides win over alias inference."""
    required, optional = _kind_fields(kind)
    known_fields = (*required, *optional)
    normalised_headers = {_normalise_header(header): header for header in headers}

    mapping: dict[str, str] = {}
    for field_name, header in (overrides or {}).items():
        if field_name not in known_fields:
            raise CsvIntakeError(f"unknown mapping field for {kind!r}: {field_name!r}")
        if header not in headers:
            raise CsvIntakeError(f"mapped column {header!r} is not in the file's header row")
        mapping[field_name] = header

    for field_name in known_fields:
        if field_name in mapping:
            continue
        for alias in _FIELD_ALIASES[field_name]:
            header = normalised_headers.get(_normalise_header(alias))
            if header is not None and header not in mapping.values():
                mapping[field_name] = header
                break

    missing = tuple(field_name for field_name in required if field_name not in mapping)
    return mapping, missing


def preview_csv(
    kind: str,
    text: str,
    *,
    tenant_id: str,
    mapping: dict[str, str] | None = None,
    default_location: str | None = None,
) -> CsvPreview:
    headers, rows = _parse_table(text)
    resolved, missing = infer_mapping(kind, headers, mapping)

    if missing:
        return CsvPreview(
            kind=kind,
            columns=tuple(headers),
            mapping=resolved,
            missing_required=missing,
            row_count=len(rows),
            valid_rows=0,
            invalid_rows=len(rows),
            issues=(),
            sample=(),
            file_warnings=(
                "no rows were validated: map the missing required columns and re-preview",
            ),
        )

    parsed = [
        _canonicalise_row(kind, row, line, resolved, tenant_id, default_location)
        for line, row in rows
    ]
    issues = tuple(
        CsvRowIssue(row=item.line, errors=item.validation.errors, warnings=item.validation.warnings)
        for item in parsed
        if item.validation.errors or item.validation.warnings
    )[:_PREVIEW_ISSUE_LIMIT]
    valid = [item for item in parsed if item.validation.ok]
    return CsvPreview(
        kind=kind,
        columns=tuple(headers),
        mapping=resolved,
        missing_required=(),
        row_count=len(parsed),
        valid_rows=len(valid),
        invalid_rows=len(parsed) - len(valid),
        issues=issues,
        sample=tuple(item.canonical for item in valid[:_PREVIEW_SAMPLE_LIMIT]),
    )


def build_records(
    kind: str,
    text: str,
    *,
    tenant_id: str,
    mapping: dict[str, str] | None = None,
    default_location: str | None = None,
) -> list[InboundRecord]:
    """One InboundRecord per data row; invalid rows carry failed validation (quarantine)."""
    headers, rows = _parse_table(text)
    resolved, missing = infer_mapping(kind, headers, mapping)
    if missing:
        raise CsvIntakeError(
            f"required columns are unmapped for {kind!r}: {', '.join(missing)}"
        )

    records: list[InboundRecord] = []
    for line, row in rows:
        parsed = _canonicalise_row(kind, row, line, resolved, tenant_id, default_location)
        records.append(
            wrap(
                tenant_id=tenant_id,
                system=SourceSystem.CSV,
                object_type=f"csv_{kind}_row",
                object_id=_object_id(kind, parsed),
                event_time=_record_event_time(parsed),
                canonical_type=_CANONICAL_TYPES[kind],
                canonical=parsed.canonical,
                validation=parsed.validation,
                raw={"line": parsed.line, **parsed.values},
            )
        )
    return records


_CANONICAL_TYPES = {
    "products": "product_master",
    "stock": "inventory_state",
    "expiry": "expiry_entry",
    "sales": "sales_line",
}


def _kind_fields(kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        return _KIND_FIELDS[kind]
    except KeyError as exc:
        raise CsvIntakeError(
            f"unknown CSV kind {kind!r}; expected one of {', '.join(CSV_INTAKE_KINDS)}"
        ) from exc


def _normalise_header(header: str) -> str:
    return " ".join(header.replace("_", " ").replace("-", " ").lower().split())


def _parse_table(text: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    if not text.strip():
        raise CsvIntakeError("the CSV file is empty")
    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = [cell.strip() for cell in next(reader)]
    except StopIteration as exc:  # pragma: no cover - guarded by the strip() check above
        raise CsvIntakeError("the CSV file is empty") from exc
    if not any(headers):
        raise CsvIntakeError("the first line must be a header row of column names")
    if len([h for h in headers if h]) != len({h for h in headers if h}):
        raise CsvIntakeError("the header row contains duplicate column names")

    rows: list[tuple[int, dict[str, str]]] = []
    for line_number, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue  # blank line, not data
        if len(rows) >= MAX_CSV_ROWS:
            raise CsvIntakeError(
                f"the file exceeds {MAX_CSV_ROWS} data rows; split it and import in parts"
            )
        values = {
            header: (cells[index].strip() if index < len(cells) else "")
            for index, header in enumerate(headers)
            if header
        }
        rows.append((line_number, values))
    return headers, rows


def _sniff_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def _canonicalise_row(
    kind: str,
    values: dict[str, str],
    line: int,
    mapping: dict[str, str],
    tenant_id: str,
    default_location: str | None,
) -> _ParsedRow:
    fields = {
        field_name: values.get(header, "") for field_name, header in mapping.items()
    }
    builder = _ROW_BUILDERS[kind]
    try:
        canonical, validation = builder(fields, tenant_id, default_location)
    except _RowError as exc:
        return _ParsedRow(
            line=line,
            values=values,
            canonical=dict(fields),
            validation=ValidationResult().fail(str(exc)),
        )
    return _ParsedRow(line=line, values=values, canonical=canonical, validation=validation)


class _RowError(ValueError):
    """A per-row parse failure: quarantines the row without aborting the file."""


def _build_product(
    fields: dict[str, str],
    tenant_id: str,
    _default_location: str | None,
) -> tuple[dict[str, Any], ValidationResult]:
    product = ProductMaster(
        tenant_id=tenant_id,
        source_system=SourceSystem.CSV,
        source_product_id=fields.get("sku") or fields.get("barcode") or fields.get("gtin") or "",
        sku=fields.get("sku") or None,
        gtin=fields.get("gtin") or None,
        barcode=fields.get("barcode") or None,
        name=fields.get("name") or None,
        category=fields.get("category") or None,
    )
    canonical = {
        **asdict(product),
        "source_system": product.source_system.value,
        "brand": fields.get("brand") or None,
        "pack_size": fields.get("pack_size") or None,
        "unit_of_measure": fields.get("unit_of_measure") or None,
    }
    return canonical, validate_product(product)


def _build_stock(
    fields: dict[str, str],
    tenant_id: str,
    default_location: str | None,
) -> tuple[dict[str, Any], ValidationResult]:
    location = fields.get("location_id") or default_location or ""
    state_raw = fields.get("stock_state", "").strip().lower()
    try:
        stock_state = StockState(state_raw) if state_raw else StockState.ON_HAND
    except ValueError as exc:
        raise _RowError(
            f"unknown stock_state {state_raw!r}; expected one of "
            f"{', '.join(state.value for state in StockState)}"
        ) from exc
    inventory = InventoryState(
        tenant_id=tenant_id,
        sku=fields.get("sku", ""),
        location_id=location,
        quantity=_decimal_field(fields.get("quantity"), "quantity"),
        stock_state=stock_state,
        event_time=_optional_datetime_field(fields.get("event_time"), "event_time"),
        gtin=fields.get("gtin") or None,
        barcode=fields.get("barcode") or None,
    )
    canonical = {
        **asdict(inventory),
        "stock_state": inventory.stock_state.value,
        "quantity": str(inventory.quantity),
        "event_time": inventory.event_time.isoformat() if inventory.event_time else None,
    }
    return canonical, validate_inventory(inventory)


def _build_expiry(
    fields: dict[str, str],
    tenant_id: str,
    default_location: str | None,
) -> tuple[dict[str, Any], ValidationResult]:
    entry = ExpiryEntry(
        tenant_id=tenant_id,
        sku=fields.get("sku", ""),
        location_id=fields.get("location_id") or default_location or "",
        quantity=_decimal_field(fields.get("quantity"), "quantity"),
        expiry_date=_date_field(fields.get("expiry_date"), "expiry_date"),
    )
    canonical = {
        **asdict(entry),
        "quantity": str(entry.quantity),
        "expiry_date": entry.expiry_date.isoformat(),
    }
    return canonical, validate_expiry(entry)


def _build_sale(
    fields: dict[str, str],
    tenant_id: str,
    default_location: str | None,
) -> tuple[dict[str, Any], ValidationResult]:
    quantity = _decimal_field(fields.get("quantity"), "quantity")
    if quantity != quantity.to_integral_value():
        raise _RowError("fractional sales quantities are not supported in CSV sales import")
    row_key = _row_content_key(fields)
    order_id = fields.get("order_id") or f"csvorder_{row_key}"
    line_id = fields.get("line_id") or f"csvline_{row_key}"
    sale = SalesLine(
        tenant_id=tenant_id,
        order_id=order_id,
        line_id=line_id,
        sku=fields.get("sku", ""),
        location_id=fields.get("location_id") or default_location or "store",
        quantity=int(quantity),
        unit_price=_money_field(fields.get("unit_price"), "unit_price"),
        sold_at=_datetime_field(fields.get("sold_at"), "sold_at"),
    )
    validation = validate_sales(sale)
    if not fields.get("order_id"):
        validation = validation.warn(
            "no order_id column: rows dedup by content only, so overlapping exports "
            "of identical rows cannot be told apart"
        )
    canonical = {
        **asdict(sale),
        "unit_price": sale.unit_price.to_dict(),
        "sold_at": sale.sold_at.isoformat(),
    }
    return canonical, validation


_ROW_BUILDERS = {
    "products": _build_product,
    "stock": _build_stock,
    "expiry": _build_expiry,
    "sales": _build_sale,
}


def _object_id(kind: str, parsed: _ParsedRow) -> str:
    canonical = parsed.canonical
    if kind == "products":
        return str(canonical.get("source_product_id") or f"line_{parsed.line}")
    if kind == "stock":
        return f"{canonical.get('sku', '')}:{canonical.get('location_id', '')}"
    if kind == "expiry":
        return (
            f"{canonical.get('sku', '')}:{canonical.get('location_id', '')}"
            f":{canonical.get('expiry_date', '')}"
        )
    return f"{canonical.get('order_id', '')}:{canonical.get('line_id', '')}"


def _record_event_time(parsed: _ParsedRow) -> datetime:
    for key in ("sold_at", "event_time"):
        value = parsed.canonical.get(key)
        if isinstance(value, str) and value:
            try:
                return _parse_datetime(value)
            except _RowError:  # pragma: no cover - canonical values are pre-validated
                break
    return now_utc()


def _row_content_key(fields: dict[str, str]) -> str:
    basis = "|".join(f"{key}={fields.get(key, '')}" for key in sorted(fields))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _decimal_field(value: str | None, field_name: str) -> Decimal:
    try:
        quantity = Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation as exc:
        raise _RowError(f"{field_name} is not numeric: {value!r}") from exc
    if not quantity.is_finite():
        raise _RowError(f"{field_name} must be finite: {value!r}")
    return quantity


def _money_field(value: str | None, field_name: str) -> Money:
    amount = _decimal_field(value, field_name)
    return Money.zar(str(amount))


def _date_field(value: str | None, field_name: str) -> date:
    if not value:
        raise _RowError(f"{field_name} is required")
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        return _parse_datetime(value).date()
    except _RowError as exc:
        raise _RowError(
            f"{field_name} must be an ISO date (e.g. 2026-07-21), got {value!r}"
        ) from exc


def _datetime_field(value: str | None, field_name: str) -> datetime:
    if not value:
        raise _RowError(f"{field_name} is required")
    return _parse_datetime(value, field_name=field_name)


def _optional_datetime_field(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    return _parse_datetime(value, field_name=field_name)


def _parse_datetime(value: str, *, field_name: str = "timestamp") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _RowError(
            f"{field_name} must be ISO format (e.g. 2026-07-21 or 2026-07-21T14:30:00), "
            f"got {value!r}"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
