"""Read-only Microsoft Dynamics 365 Business Central item-inventory connector."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from ...canonical import InventoryState, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_inventory
from ..poll import InMemoryCursorStore, PollingConnector
from ._common import FetchJson, http_get_json, now_utc, parse_time, wrap


def map_dynamics_inventory(
    row: dict,
    *,
    tenant_id: str,
    location_id: str,
) -> InboundRecord:
    """Map one Business Central `items` resource into a stock snapshot.

    Business Central's standard item API exposes `number`, `inventory`, `gtin`, and
    `lastModifiedDateTime`; it does not include warehouse grain.  The connector's
    configured location therefore names the inventory scope and is required rather
    than silently assigning an arbitrary store.
    """
    observed_at = parse_time(row.get("lastModifiedDateTime"))
    sku = str(row.get("number") or "").strip()
    item_id = str(row.get("id") or sku or "unknown")
    if not sku:
        return _invalid_record(
            row,
            tenant_id=tenant_id,
            object_id=item_id,
            event_time=observed_at,
            error="dynamics item has no number",
        )
    try:
        inventory = InventoryState(
            tenant_id=tenant_id,
            sku=sku,
            location_id=location_id,
            quantity=Decimal(str(row.get("inventory", "0"))),
            event_time=observed_at,
            gtin=str(row["gtin"]) if row.get("gtin") else None,
            barcode=str(row["gtin"]) if row.get("gtin") else None,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        return _invalid_record(
            row,
            tenant_id=tenant_id,
            object_id=item_id,
            event_time=observed_at,
            error=f"dynamics item inventory is malformed: {exc}",
        )
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.DYNAMICS,
        object_type="inventory_state",
        object_id=f"{item_id}@{location_id}",
        event_time=observed_at,
        canonical_type="inventory_state",
        canonical=inventory,
        validation=validate_inventory(inventory),
        raw=row,
    )


class DynamicsBusinessCentralInventoryConnector(PollingConnector):
    """Poll one configured Business Central `items` collection with OAuth bearer auth."""

    source_system = SourceSystem.DYNAMICS

    def __init__(
        self,
        cursors: InMemoryCursorStore,
        *,
        base_url: str,
        token: str,
        location_id: str,
        tenant_id: str,
        fetch_json: FetchJson = http_get_json,
    ) -> None:
        super().__init__(cursors, tenant_id=tenant_id)
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._location_id = location_id
        self._tenant_id = tenant_id
        self._fetch_json = fetch_json

    async def fetch_page(
        self,
        cursor: str | None,
    ) -> tuple[list[InboundRecord], str | None]:
        # Business Central returns a fully qualified @odata.nextLink.  Reuse it as-is;
        # rebuilding its query would drop opaque server paging state.
        url = cursor if cursor and cursor.startswith(("https://", "http://")) else self._base_url
        params = {} if cursor and cursor.startswith(("https://", "http://")) else {"$top": "200"}
        body = await self._fetch_json(
            url,
            params,
            {"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        rows = body.get("value") if isinstance(body.get("value"), list) else []
        records = [
            map_dynamics_inventory(row, tenant_id=self._tenant_id, location_id=self._location_id)
            for row in rows
            if isinstance(row, dict)
        ]
        next_link = body.get("@odata.nextLink") or body.get("nextLink") or body.get("next_cursor")
        return records, str(next_link) if next_link else None


def _invalid_record(
    row: dict,
    *,
    tenant_id: str,
    object_id: str,
    event_time: object,
    error: str,
) -> InboundRecord:
    timestamp = event_time if hasattr(event_time, "tzinfo") else now_utc()
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.DYNAMICS,
        object_type="inventory_state",
        object_id=object_id,
        event_time=timestamp,
        canonical_type="inventory_state",
        canonical={},
        validation=ValidationResult().fail(error),
        raw=row,
    )
