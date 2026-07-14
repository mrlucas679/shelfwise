from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from random import Random
from typing import Any

from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_runtime.provenance import DataDomain

from .catalog.sample import sample_assortment


def seed_int(seed: int, value: str) -> int:
    """Derive a stable integer from a world seed and a domain value."""
    digest = hashlib.blake2b(f"{seed}:{value}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def demand_multiplier(current: date) -> float:
    """Apply a small calendar effect without relying on a planted product story."""
    multiplier = 1.28 if current.day >= 25 or current.day <= 3 else 1.0
    if current.weekday() in {4, 5}:
        multiplier += 0.15
    return multiplier


@dataclass(frozen=True, slots=True)
class EventTypeRoute:
    """Document whether a canonical event is consumed now or intentionally stored."""

    consumer: str | None = None
    stored_only: bool = False
    reason: str = ""


EVENT_TYPE_ROUTES: dict[EventType, EventTypeRoute] = {
    EventType.SCAN: EventTypeRoute(consumer="golden_expiry_cascade"),
    EventType.SALE: EventTypeRoute(consumer="sales_or_catalog_price_cascade"),
    EventType.COLD_CHAIN_ALERT: EventTypeRoute(consumer="cold_chain_cascade"),
    EventType.STOCK_UPDATE: EventTypeRoute(
        stored_only=True,
        reason="inventory snapshot retained for joins and later demand reasoning",
    ),
    EventType.EXPIRY_ENTRY: EventTypeRoute(consumer="expiry_risk_cascade"),
    EventType.SUPPLIER_UPDATE: EventTypeRoute(consumer="procurement_cascade"),
    EventType.SHIPMENT: EventTypeRoute(
        stored_only=True,
        reason="fulfilment fact retained until shipment reconciliation consumes it",
    ),
    EventType.RECALL_NOTICE: EventTypeRoute(consumer="recall_quarantine_cascade"),
    EventType.INVENTORY_EXCEPTION: EventTypeRoute(consumer="inventory_exception_cascade"),
}


@dataclass(frozen=True, slots=True)
class WorldConfig:
    seed: int
    start: date = date(2026, 6, 22)
    days: int = 7
    scenario_id: str = "ad_hoc"
    tenant_id: str = "local"
    store_id: str = "store_obs_main"
    area: str = "observatory_blk7"
    stage: int = 4
    incident_days: tuple[int, ...] = (0,)
    products: Sequence[object] | None = None


class World:
    """Deterministic synthetic retail world that emits only canonical events."""

    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.products = tuple(cfg.products or sample_assortment(cfg.seed, size=24))

    def run(self) -> Iterator[Event]:
        """Emit events in timestamp order for the configured week."""
        events: list[Event] = []
        for day_index in range(self.cfg.days):
            events.extend(self._day(day_index))
        ordered = sorted(events, key=lambda event: (event.ts, event.id))
        _assert_unique_event_ids(ordered)
        yield from ordered

    def _day(self, day_index: int) -> list[Event]:
        """Generate one retail day of stock, sales, expiry, and shipment events."""
        current = self.cfg.start + timedelta(days=day_index)
        rng = Random(seed_int(self.cfg.seed, current.isoformat()))
        events: list[Event] = []
        for product in self.products:
            opening = self._opening_stock(product)
            sold = self._daily_sales(rng, current, product)
            ts_base = datetime.combine(current, time(8), tzinfo=UTC)
            events.append(
                self._mk(
                    EventType.STOCK_UPDATE,
                    ts_base,
                    EventSource.WMS_CSV,
                    {
                        "store_id": self.cfg.store_id,
                        "sku": _sku(product),
                        "product": _name(product),
                        "on_hand": opening,
                        "reorder_point": self._reorder_point(product),
                    },
                )
            )
            events.append(
                self._mk(
                    EventType.SALE,
                    ts_base + timedelta(hours=3, minutes=rng.randint(0, 90)),
                    EventSource.POS_CSV,
                    {
                        "store_id": self.cfg.store_id,
                        "sku": _sku(product),
                        "units": sold,
                        "unit_price_cents": _till_price(product, rng),
                        "catalog_price_cents": _catalog_price(product),
                    },
                )
            )
            if _cat(product).refrigerated:
                events.append(
                    self._mk(
                        EventType.EXPIRY_ENTRY,
                        ts_base + timedelta(hours=5),
                        EventSource.WMS_CSV,
                        {
                            "store_id": self.cfg.store_id,
                            "sku": _sku(product),
                            "batch_id": f"B{day_index:02d}-{_sku(product)}",
                            "category": _physics_name(product),
                            "storage": _cat(product).storage,
                            "days_to_expiry": max(_cat(product).shelf_life_days - day_index - 4, 0),
                        },
                    )
                )
            if opening - sold <= self._reorder_point(product):
                self._reorder(events, current, product)
        events.extend(self._control_events(current, day_index))
        return events

    def _control_events(self, current: date, day_index: int) -> list[Event]:
        """Emit factual probes that keep every canonical stream lane exercised.

        These are observations, not recommendations or expected answers. They make a
        bounded stream sample representative even when a generated assortment contains
        thousands of same-timestamp stock snapshots.
        """
        ts_base = datetime.combine(current, time(9), tzinfo=UTC)
        events: list[Event] = []
        if day_index == 0:
            misprice_product = self.products[1] if len(self.products) > 1 else self.products[0]
            hero_product = self.products[0]
            expiry_product = next(
                (product for product in self.products if _cat(product).refrigerated), hero_product
            )
            hero_sku = _sku(hero_product)
            expiry_sku = _sku(expiry_product)
            hero_units = self._opening_stock(hero_product)
            hero_supplier = str(getattr(hero_product, "supplier", f"supplier_{hero_sku}"))
            misprice_catalog = max(_catalog_price(misprice_product), 100)
            events.extend(
                [
                    self._mk(
                        EventType.SCAN,
                        ts_base,
                        EventSource.SCANNER,
                        {
                            "store_id": self.cfg.store_id,
                            "location": self.cfg.store_id,
                            "sku": hero_sku,
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.SALE,
                        ts_base + timedelta(hours=1),
                        EventSource.POS_CSV,
                        {
                            "store_id": self.cfg.store_id,
                            "sku": _sku(misprice_product),
                            "units": 20,
                            "unit_price_cents": max(1, misprice_catalog // 2),
                            "catalog_price_cents": misprice_catalog,
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.EXPIRY_ENTRY,
                        ts_base + timedelta(hours=3, minutes=45),
                        EventSource.WMS_CSV,
                        {
                            "store_id": self.cfg.store_id,
                            "sku": expiry_sku,
                            "batch_id": f"B-PROBE-{current:%Y%m%d}-{expiry_sku}",
                            "category": _physics_name(expiry_product),
                            "storage": _cat(expiry_product).storage,
                            "days_to_expiry": 1,
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.SUPPLIER_UPDATE,
                        ts_base + timedelta(hours=6, minutes=30),
                        EventSource.API,
                        {
                            "store_id": self.cfg.store_id,
                            "sku": hero_sku,
                            "supplier": hero_supplier,
                            "lead_time_days": 2 + seed_int(self.cfg.seed, hero_supplier) % 3,
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.SHIPMENT,
                        ts_base + timedelta(hours=7),
                        EventSource.API,
                        {
                            "store_id": self.cfg.store_id,
                            "sku": hero_sku,
                            "ordered_units": max(1, self._reorder_point(hero_product) * 2),
                            "eta": (current + timedelta(days=2)).isoformat(),
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.RECALL_NOTICE,
                        ts_base + timedelta(hours=7, minutes=15),
                        EventSource.API,
                        {
                            "store_id": self.cfg.store_id,
                            "location": self.cfg.store_id,
                            "recall_id": f"REC-PROBE-{current:%Y%m%d}",
                            "sku": hero_sku,
                            "lot_id": f"B-PROBE-{current:%Y%m%d}",
                            "units": 10,
                            "reason": "synthetic supplier recall drill",
                            "issued_by": f"{hero_supplier} Quality",
                            "synthetic_probe": True,
                        },
                    ),
                    self._mk(
                        EventType.INVENTORY_EXCEPTION,
                        ts_base + timedelta(hours=7, minutes=30),
                        EventSource.MANUAL,
                        {
                            "store_id": self.cfg.store_id,
                            "exception_id": f"EXC-PROBE-{current:%Y%m%d}",
                            "exception_type": "shrink",
                            "sku": hero_sku,
                            "reason": "synthetic cycle-count discrepancy",
                            "location": self.cfg.store_id,
                            "expected_units": hero_units,
                            "counted_units": max(0, hero_units - max(1, hero_units // 10)),
                            "count_reference": f"COUNT-PROBE-{current:%Y%m%d}",
                            "synthetic_probe": True,
                        },
                    ),
                ]
            )
        if day_index in self.cfg.incident_days:
            events.append(
                self._mk(
                    EventType.COLD_CHAIN_ALERT,
                    ts_base + timedelta(hours=6),
                    EventSource.API,
                    {
                        "site_id": self.cfg.store_id,
                        "asset_id": (
                            f"cold-chain:{self.cfg.store_id}:"
                            f"{_physics_name(self.products[0])}"
                        ),
                        "category": _physics_name(self.products[0]),
                        "diagnosis": "generator_failed",
                        "severity": 2,
                        "predicted_minutes_to_unsafe": "18",
                        "measured_outage_hours": "4",
                        "stock_at_risk": {
                            "minor_units": _catalog_price(self.products[0])
                            * self._opening_stock(self.products[0]),
                            "currency": str(getattr(self.products[0], "currency", "ZAR")),
                        },
                        "synthetic_probe": True,
                    },
                )
            )
        return events

    def _reorder(self, events: list[Event], current: date, product: object) -> None:
        """Emit supplier and shipment events when simulated stock crosses reorder point."""
        ts_base = datetime.combine(current, time(16), tzinfo=UTC)
        sku = _sku(product)
        events.append(
            self._mk(
                EventType.SUPPLIER_UPDATE,
                ts_base,
                EventSource.API,
                {
                    "store_id": self.cfg.store_id,
                    "sku": sku,
                    "supplier": f"supplier_{sku}",
                    "lead_time_days": 2 + seed_int(self.cfg.seed, sku) % 3,
                },
            )
        )
        events.append(
            self._mk(
                EventType.SHIPMENT,
                ts_base + timedelta(minutes=30),
                EventSource.API,
                {
                    "store_id": self.cfg.store_id,
                    "sku": sku,
                    "ordered_units": self._reorder_point(product) * 2,
                    "eta": (current + timedelta(days=2)).isoformat(),
                },
            )
        )

    def _mk(
        self,
        event_type: EventType,
        ts: datetime,
        source: EventSource,
        payload: dict,
    ) -> Event:
        """Build a deterministic canonical event."""
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        raw = "|".join(
            (
                self.cfg.tenant_id,
                self.cfg.scenario_id,
                str(self.cfg.seed),
                event_type.value,
                ts.isoformat(),
                payload_json,
            )
        )
        # crc32 is 32 bits: at ~750k events/run the birthday bound guarantees thousands of
        # id collisions, which cascade into decision-id collisions and bogus "duplicates".
        # blake2b at 96 bits is deterministic and collision-free at any realistic scale.
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
        event_id = f"evt_{digest}"
        return Event(
            id=event_id,
            type=event_type,
            ts=ts,
            actor=self.cfg.store_id,
            payload=payload,
            source=source,
            tenant_id=self.cfg.tenant_id,
            data_domain=DataDomain.WORLD_SIMULATION,
            correlation_id=f"world_{self.cfg.scenario_id}_{self.cfg.seed}",
        )

    def _opening_stock(self, product: object) -> int:
        """Create deterministic opening stock for a product."""
        return _cat(product).base_daily_units * (4 + seed_int(self.cfg.seed, _sku(product)) % 4)

    def _reorder_point(self, product: object) -> int:
        """Create a conservative reorder point from base demand."""
        return max(10, _cat(product).base_daily_units * 2)

    def _daily_sales(self, rng: Random, current: date, product: object) -> int:
        """Calculate daily unit sales with calendar uplift and small noise."""
        base = _cat(product).base_daily_units
        noise = rng.uniform(0.85, 1.15)
        return max(1, round(base * demand_multiplier(current) * noise))


def _cat(product: object):
    """Return the product physics/category object through the common duck type."""
    return product.cat


def _sku(product: object) -> str:
    """Return a stable SKU from catalog or ground-truth products."""
    return str(product.sku)


def _name(product: object) -> str:
    """Return a display name from catalog or ground-truth products."""
    return str(getattr(product, "name", getattr(product, "generic_name", _sku(product))))


def _physics_name(product: object) -> str:
    """Return the cold-chain physics key for either product implementation."""
    if hasattr(product, "physics"):
        return str(product.physics)
    return str(_cat(product).name)


def _price(product: object, rng: Random) -> int:
    """Choose a deterministic shelf price inside a product's price band."""
    low = int(getattr(product, "price_low_c", getattr(product, "price_cents", 100)))
    high = int(getattr(product, "price_high_c", getattr(product, "price_cents", low)))
    return rng.randint(min(low, high), max(low, high))


MISPRICE_RATE = 0.02
_MISPRICE_FACTORS = (0.55, 0.65, 1.45, 1.6)


def _till_price(product: object, rng: Random) -> int:
    """Observed till price: normal band variance, with rare deterministic mispricing.

    Most sales stay inside the product's normal price band; a small, seed-driven
    fraction land far outside it (a stale shelf label, a fat-fingered override).
    The world only emits the odd number - deciding whether it is a problem is the
    application's job, never the simulator's.
    """
    price = _price(product, rng)
    if rng.random() < MISPRICE_RATE:
        price = max(99, int(price * rng.choice(_MISPRICE_FACTORS)))
    return price


def _catalog_price(product: object) -> int:
    """Return the master-data catalogue price for either product implementation."""
    return int(getattr(product, "price_cents", 0))


def span_event_stream(events: Sequence[Event], limit: int) -> list[Event]:
    """Sample chronologically across a stream while preserving every event type.

    The first occurrence of each present type is reserved, then the remaining slots are
    filled at deterministic intervals across the complete stream. A caller cannot claim
    full event-type coverage with fewer slots than the stream requires.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = list(events)
    if len(rows) <= limit:
        return rows

    first_by_type: dict[EventType, int] = {}
    for index, event in enumerate(rows):
        first_by_type.setdefault(event.type, index)
    if len(first_by_type) > limit:
        raise ValueError(f"limit {limit} cannot cover {len(first_by_type)} event types")

    selected = set(first_by_type.values())
    for slot in range(limit):
        if len(selected) >= limit:
            break
        selected.add(min(int(slot * len(rows) / limit), len(rows) - 1))
    if len(selected) < limit:
        for index in range(len(rows)):
            selected.add(index)
            if len(selected) >= limit:
                break
    return [rows[index] for index in sorted(selected)[:limit]]


def assert_world_event_contract(
    events: Sequence[Event],
    *,
    require_all_types: bool = True,
) -> dict[str, Any]:
    """Fail on duplicate ids, missing event lanes, or an unowned event type."""
    rows = list(events)
    _assert_unique_event_ids(rows)
    missing_routes = set(EventType) - set(EVENT_TYPE_ROUTES)
    invalid_routes = {
        event_type
        for event_type, route in EVENT_TYPE_ROUTES.items()
        if bool(route.consumer) == bool(route.stored_only)
        or (not route.reason and route.stored_only)
    }
    if missing_routes or invalid_routes:
        raise AssertionError(
            "invalid event route contract: "
            f"missing={sorted(item.value for item in missing_routes)} "
            f"invalid={sorted(item.value for item in invalid_routes)}"
        )
    present = {event.type for event in rows}
    missing_types = set(EventType) - present
    if require_all_types and missing_types:
        raise AssertionError(
            f"world stream missing event types: {sorted(item.value for item in missing_types)}"
        )
    return {
        "events": len(rows),
        "event_types": sorted(item.value for item in present),
        "consumers": {
            event_type.value: route.consumer
            for event_type, route in EVENT_TYPE_ROUTES.items()
            if route.consumer
        },
        "stored_only": {
            event_type.value: route.reason
            for event_type, route in EVENT_TYPE_ROUTES.items()
            if route.stored_only
        },
    }


def _assert_unique_event_ids(events: Sequence[Event]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for event in events:
        if event.id in seen:
            duplicates.add(event.id)
        seen.add(event.id)
    if duplicates:
        raise AssertionError(f"world emitted duplicate event ids: {sorted(duplicates)}")


__all__ = [
    "EVENT_TYPE_ROUTES",
    "EventTypeRoute",
    "World",
    "WorldConfig",
    "assert_world_event_contract",
    "span_event_stream",
]
