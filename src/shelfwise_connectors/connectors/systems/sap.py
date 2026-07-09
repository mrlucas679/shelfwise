from __future__ import annotations

from decimal import Decimal

from ...canonical import InventoryState, SourceSystem
from ...provenance import InboundRecord
from ...validation import validate_inventory
from ..poll import InMemoryCursorStore, PollingConnector
from ._common import FetchJson, http_get_json, now_utc, wrap


def map_sap_inventory(row: dict, *, tenant_id: str) -> InboundRecord:
    now = now_utc()
    material = str(row["Material"])
    location = str(row["StorageLocation"])
    inventory = InventoryState(
        tenant_id=tenant_id,
        sku=material,
        location_id=location,
        quantity=Decimal(str(row.get("MatlWrhsStkQtyInMatlBaseUnit", 0))),
        event_time=now,
    )
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.SAP,
        object_type="inventory_state",
        object_id=f"{material}@{location}",
        event_time=now,
        canonical_type="inventory_state",
        canonical=inventory,
        validation=validate_inventory(inventory),
        raw=row,
    )


class SapS4InventoryConnector(PollingConnector):
    source_system = SourceSystem.SAP

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
        params = {"$top": "200"}
        if cursor:
            params["$skiptoken"] = cursor
        body = await self._fetch_json(
            f"{self._base_url}/MaterialStock",
            params,
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        rows = body.get("value") if isinstance(body.get("value"), list) else []
        records = [map_sap_inventory(row, tenant_id=self._tenant_id) for row in rows]
        return records, _next_cursor(body)


def _next_cursor(body: dict) -> str | None:
    token = body.get("@odata.nextLink") or body.get("nextLink") or body.get("next_cursor")
    return str(token) if token else None
