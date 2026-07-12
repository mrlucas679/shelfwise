from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from shelfwise_connectors import TaskWriteBackSink, raw_payload_hash
from shelfwise_contracts import Event, EventSource, EventType, RecommendedAction

from .seed import (
    DEFAULT_DATASETS,
    REFERENCE_NOW,
    load_sales,
    load_stock,
    load_suppliers,
)

TENANT_ID = os.getenv("SHELFWISE_TENANT_ID") or os.getenv("TENANT_ID") or "local"


class CsvConnector:
    """Async adapter over the local CSV exports used by the backend demo."""

    def __init__(
        self,
        datasets_dir: Path = DEFAULT_DATASETS,
        *,
        now: datetime = REFERENCE_NOW,
        tenant_id: str = TENANT_ID,
    ) -> None:
        self._dir = Path(datasets_dir)
        self._now = now
        self._tenant_id = tenant_id
        self._sink = TaskWriteBackSink()

    async def read_export(self, kind: str) -> AsyncIterator[Event]:
        """Yield traceable events for a named CSV export kind."""

        builders = {
            "stock": self._stock_events,
            "expiry": self._expiry_events,
            "sales": self._sale_events,
            "suppliers": self._supplier_events,
        }
        builder = builders.get(kind)
        if builder is None:
            raise ValueError(f"unknown export kind: {kind!r}")
        for event in builder():
            yield event

    async def write_back(
        self,
        action: RecommendedAction,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create an idempotent task rather than editing the source CSVs."""

        return self._sink.create_task(
            idempotency_key=idempotency_key,
            tenant_id=self._tenant_id,
            title=f"Review {action.type}",
            action=action.to_dict(),
        )

    def _stock_events(self) -> list[Event]:
        events: list[Event] = []
        for index, stock in enumerate(load_stock(self._dir / "stock.csv")):
            payload = {
                "sku": stock.sku,
                "location": stock.location,
                "on_hand": stock.on_hand,
                "reorder_point": stock.reorder_point,
                "raw_payload_hash": _hash_row(asdict(stock)),
            }
            events.append(
                self._event(
                    f"evt_stock_{index}",
                    EventType.STOCK_UPDATE,
                    stock.location,
                    EventSource.WMS_CSV,
                    payload,
                )
            )
        return events

    def _expiry_events(self) -> list[Event]:
        events: list[Event] = []
        for index, stock in enumerate(load_stock(self._dir / "stock.csv")):
            payload = {
                "sku": stock.sku,
                "location": stock.location,
                "on_hand": stock.on_hand,
                "expiry_date": stock.expiry_date.isoformat(),
                "raw_payload_hash": _hash_row(asdict(stock)),
            }
            events.append(
                self._event(
                    f"evt_expiry_{index}",
                    EventType.EXPIRY_ENTRY,
                    stock.location,
                    EventSource.WMS_CSV,
                    payload,
                )
            )
        return events

    def _sale_events(self) -> list[Event]:
        events: list[Event] = []
        for index, sale in enumerate(load_sales(self._dir / "sales.csv")):
            payload = {
                "sku": sale.sku,
                "location": sale.location,
                "quantity": sale.quantity,
                "unit_price": str(sale.unit_price),
                "raw_payload_hash": _hash_row(asdict(sale)),
            }
            events.append(
                Event(
                    id=f"evt_sale_{index}",
                    type=EventType.SALE,
                    ts=sale.ts,
                    actor=sale.location,
                    source=EventSource.POS_CSV,
                    tenant_id=self._tenant_id,
                    payload=payload,
                )
            )
        return events

    def _supplier_events(self) -> list[Event]:
        events: list[Event] = []
        for index, supplier in enumerate(load_suppliers(self._dir / "suppliers.csv")):
            payload = {
                "supplier": supplier.supplier,
                "avg_lead_time_days": str(supplier.avg_lead_time_days),
                "recent_delay": supplier.recent_delay,
                "raw_payload_hash": _hash_row(asdict(supplier)),
            }
            events.append(
                self._event(
                    f"evt_supplier_{index}",
                    EventType.SUPPLIER_UPDATE,
                    "procurement",
                    EventSource.MANUAL,
                    payload,
                )
            )
        return events

    def _event(
        self,
        event_id: str,
        event_type: EventType,
        actor: str,
        source: EventSource,
        payload: dict[str, Any],
    ) -> Event:
        return Event(
            id=event_id,
            type=event_type,
            ts=self._now,
            actor=actor,
            source=source,
            payload=payload,
            tenant_id=self._tenant_id,
        )


def _hash_row(row: dict[str, Any]) -> str:
    return raw_payload_hash({key: str(value) for key, value in row.items()})
