from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Physics:
    storage: str
    refrigerated: bool
    shelf_life_days: int
    base_daily_units: int


PHYSICS: dict[str, Physics] = {
    "dairy": Physics("chilled", True, 10, 40),
    "processed_meat": Physics("chilled", True, 12, 22),
    "meat": Physics("chilled", True, 4, 20),
    "poultry": Physics("chilled", True, 4, 20),
    "seafood": Physics("chilled", True, 3, 12),
    "deli": Physics("chilled", True, 5, 18),
    "chilled_other": Physics("chilled", True, 14, 18),
    "frozen": Physics("frozen", True, 180, 16),
    "produce": Physics("ambient", False, 5, 48),
    "bakery": Physics("ambient", False, 4, 60),
    "eggs": Physics("ambient", False, 21, 26),
    "beverage": Physics("ambient", False, 270, 34),
    "ambient_long": Physics("ambient", False, 540, 30),
    "nonfood": Physics("ambient", False, 1080, 12),
    "health": Physics("ambient", False, 720, 10),
}
CHILLED = frozenset(key for key, value in PHYSICS.items() if value.storage == "chilled")
FROZEN = frozenset(key for key, value in PHYSICS.items() if value.storage == "frozen")
