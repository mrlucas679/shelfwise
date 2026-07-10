from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


def seed_int(*parts: object) -> int:
    """Create a stable integer seed from arbitrary values."""
    return zlib.crc32("|".join(str(part) for part in parts).encode())


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    storage: str
    refrigerated: bool
    shelf_life_days: int
    base_daily_units: int


@dataclass(frozen=True, slots=True)
class Product:
    sku: str
    name: str
    category: str
    price_cents: int
    cost_cents: int

    @property
    def cat(self) -> Category:
        return CATEGORIES[self.category]

    @property
    def price_low_c(self) -> int:
        return int(self.price_cents * 0.9)

    @property
    def price_high_c(self) -> int:
        return int(self.price_cents * 1.1)


CATEGORIES: dict[str, Category] = {
    "dairy": Category("dairy", "chilled", True, 10, 40),
    "frozen": Category("frozen", "frozen", True, 180, 16),
    "produce": Category("produce", "ambient", False, 5, 48),
    "bakery": Category("bakery", "ambient", False, 4, 60),
    "ambient_long": Category("ambient_long", "ambient", False, 540, 30),
}
PRODUCTS: tuple[Product, ...] = (
    Product("4011", "Plain Yoghurt 1L", "dairy", 2_000, 1_200),
    Product("4020", "Amasi 2L", "dairy", 3_900, 2_500),
    Product("5100", "Frozen Chicken 1kg", "frozen", 6_900, 5_200),
    Product("6100", "Brown Bread", "bakery", 1_699, 1_100),
    Product("7100", "Bananas per kg", "produce", 1_899, 1_200),
    Product("8100", "Maize Meal 5kg", "ambient_long", 8_499, 6_200),
)


def is_payday(day: date) -> bool:
    """Approximate payday uplift windows around month-end and the first day."""
    return day.day >= 25 or day.day <= 1


def is_sassa_window(day: date) -> bool:
    """Approximate SASSA grant collection demand windows."""
    return 3 <= day.day <= 7


def season(day: date) -> str:
    """Return a South African retail season label."""
    if day.month in {12, 1, 2}:
        return "summer"
    if day.month in {6, 7, 8}:
        return "winter"
    return "shoulder"


def demand_multiplier(day: date) -> float:
    """Return deterministic calendar demand uplift for the simulator."""
    multiplier = 1.0
    if is_payday(day):
        multiplier += 0.28
    if is_sassa_window(day):
        multiplier += 0.12
    if day.weekday() in {4, 5}:
        multiplier += 0.18
    if season(day) == "summer":
        multiplier += 0.08
    return round(multiplier, 3)


def slots_per_day(stage: int) -> int:
    """Map load-shedding stage into two-hour outage slots per day."""
    return max(0, min(6, stage))


def load_shedding_schedule(
    seed: int,
    *,
    area: str,
    start: date,
    days: int,
    stage: int,
) -> list[dict]:
    """Build a labeled external schedule for the director, not for emitted events."""
    slots = slots_per_day(stage)
    schedule: list[dict] = []
    for day_index in range(days):
        current = start + timedelta(days=day_index)
        for slot_index in range(slots):
            hour = (seed_int(seed, area, current, slot_index) % 11) * 2
            begins = datetime.combine(current, time(hour=hour))
            schedule.append(
                {
                    "area": area,
                    "stage": stage,
                    "day_index": day_index,
                    "start": begins.isoformat(),
                    "end": (begins + timedelta(hours=2)).isoformat(),
                    "synthetic": True,
                }
            )
    return sorted(schedule, key=lambda row: row["start"])
