from __future__ import annotations

from ...canonical import ProductMaster, SourceSystem
from ...provenance import InboundRecord
from ...validation import validate_product
from ..poll import InMemoryCursorStore, PollingConnector
from ._common import PostJson, http_post_json, now_utc, wrap


def map_odoo_product(record: dict, *, tenant_id: str) -> InboundRecord:
    product = ProductMaster(
        tenant_id=tenant_id,
        source_system=SourceSystem.ODOO,
        source_product_id=str(record["id"]),
        sku=str(record.get("default_code") or record["id"]),
        gtin=str(record["barcode"]) if record.get("barcode") else None,
        barcode=str(record["barcode"]) if record.get("barcode") else None,
        name=str(record.get("name") or "unknown"),
        category=str(record["category"]) if record.get("category") else None,
    )
    return wrap(
        tenant_id=tenant_id,
        system=SourceSystem.ODOO,
        object_type="product",
        object_id=str(record["id"]),
        event_time=now_utc(),
        canonical_type="product",
        canonical=product,
        validation=validate_product(product),
        raw=record,
    )


class OdooProductConnector(PollingConnector):
    source_system = SourceSystem.ODOO

    def __init__(
        self,
        cursors: InMemoryCursorStore,
        *,
        base_url: str,
        database: str,
        uid: int,
        api_key: str,
        tenant_id: str,
        post_json: PostJson = http_post_json,
    ) -> None:
        super().__init__(cursors, tenant_id=tenant_id)
        self._base_url = base_url.rstrip("/")
        self._database = database
        self._uid = uid
        self._api_key = api_key
        self._tenant_id = tenant_id
        self._post_json = post_json

    async def fetch_page(
        self,
        cursor: str | None,
    ) -> tuple[list[InboundRecord], str | None]:
        payload = _search_read_payload(
            database=self._database,
            uid=self._uid,
            api_key=self._api_key,
            cursor=cursor,
        )
        body = await self._post_json(
            f"{self._base_url}/jsonrpc",
            payload,
            {"Content-Type": "application/json"},
        )
        rows = _result_rows(body)
        records = [map_odoo_product(row, tenant_id=self._tenant_id) for row in rows]
        return records, _max_write_date(rows)


def _search_read_payload(
    *,
    database: str,
    uid: int,
    api_key: str,
    cursor: str | None,
) -> dict:
    domain = [["write_date", ">", cursor]] if cursor else []
    return {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                database,
                uid,
                api_key,
                "product.product",
                "search_read",
                [domain],
                {
                    "fields": ["id", "default_code", "barcode", "name", "category", "write_date"],
                    "limit": 200,
                    "order": "write_date asc",
                },
            ],
        },
        "id": "shelfwise_odoo_product_poll",
    }


def _result_rows(body: dict) -> list[dict]:
    result = body.get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict) and isinstance(result.get("records"), list):
        return [row for row in result["records"] if isinstance(row, dict)]
    return []


def _max_write_date(rows: list[dict]) -> str | None:
    values = [str(row["write_date"]) for row in rows if row.get("write_date")]
    return max(values) if values else None
