from __future__ import annotations

from decimal import Decimal

from ...canonical import InventoryState, SourceSystem
from ...provenance import InboundRecord, ValidationResult
from ...validation import validate_inventory
from ..poll import InMemoryCursorStore, PollingConnector
from ._common import FetchJson, http_get_json, now_utc, wrap


def map_syspro_inventory(row: dict, *, tenant_id: str) -> InboundRecord:
    now = now_utc()
    sku = _first(row, "StockCode", "stock_code", "stockCode", "sku")
    location = _first(row, "Warehouse", "warehouse", "WarehouseToUse", "location_id")
    if not sku or not location:
        return wrap(
            tenant_id=tenant_id,
            system=SourceSystem.SYSPRO,
            object_type="inventory_state",
            object_id=str(row.get("id") or row.get("StockCode") or "unknown"),
            event_time=now,
            canonical_type="inventory_state",
            canonical={},
            validation=ValidationResult().fail(
                "syspro inventory row has no stock code or warehouse"
            ),
            raw=row,
        )
    inventory = InventoryState(
        tenant_id=tenant_id,
        sku=str(sku),
        location_id=str(location),
        quantity=Decimal(str(_first(row, "QtyOnHand", "QtyAvailable", "quantity", default="0"))),
        event_time=now,
    )
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.SYSPRO,
        object_type="inventory_state",
        object_id=f"{sku}@{location}",
        event_time=now,
        canonical_type="inventory_state",
        canonical=inventory,
        validation=validate_inventory(inventory),
        raw=row,
    )


class SysproInventoryConnector(PollingConnector):
    source_system = SourceSystem.SYSPRO

    def __init__(
        self,
        cursors: InMemoryCursorStore,
        *,
        base_url: str,
        token: str,
        tenant_id: str,
        fetch_json: FetchJson = http_get_json,
    ) -> None:
        super().__init__(cursors, tenant_id=tenant_id)
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._tenant_id = tenant_id
        self._fetch_json = fetch_json

    async def fetch_page(
        self,
        cursor: str | None,
    ) -> tuple[list[InboundRecord], str | None]:
        params = {"limit": "200"}
        if cursor:
            params["cursor"] = cursor
        body = await self._fetch_json(
            f"{self._base_url}/inventory",
            params,
            {"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        rows = body.get("items") or body.get("value") or []
        clean_rows = [row for row in rows if isinstance(row, dict)]
        return [
            map_syspro_inventory(row, tenant_id=self._tenant_id) for row in clean_rows
        ], _next_cursor(body)


def _first(row: dict, *keys: str, default: object = "") -> object:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _next_cursor(body: dict) -> str | None:
    token = body.get("next_cursor") or body.get("nextCursor") or body.get("cursor")
    return str(token) if token else None
