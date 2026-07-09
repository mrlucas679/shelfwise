from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from random import Random

from shelfwise_contracts import Event, EventSource, EventType

from .sa_ground_truth import PRODUCTS, demand_multiplier, seed_int


@dataclass(frozen=True, slots=True)
class WorldConfig:
    seed: int
    start: date = date(2026, 6, 22)
    days: int = 7
    tenant_id: str = "sa_retail_demo"
    store_id: str = "store_obs_main"
    area: str = "observatory_blk7"
    stage: int = 4
    products: Sequence[object] | None = None


class World:
    """Deterministic synthetic retail world that emits only canonical events."""

    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.products = tuple(cfg.products or PRODUCTS)

    def run(self) -> Iterator[Event]:
        """Emit events in timestamp order for the configured week."""
        events: list[Event] = []
        for day_index in range(self.cfg.days):
            events.extend(self._day(day_index))
        yield from sorted(events, key=lambda event: (event.ts, event.id))

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
                        "unit_price_cents": _price(product, rng),
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
        raw = f"{self.cfg.seed}|{event_type.value}|{ts.isoformat()}|{payload}"
        event_id = f"evt_{seed_int(raw):08x}"
        return Event(
            id=event_id,
            type=event_type,
            ts=ts,
            actor=self.cfg.store_id,
            payload=payload,
            source=source,
            tenant_id=self.cfg.tenant_id,
            correlation_id=f"world_{self.cfg.seed}",
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


__all__ = ["World", "WorldConfig"]
