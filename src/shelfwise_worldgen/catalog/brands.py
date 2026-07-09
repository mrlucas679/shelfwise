from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Brand:
    name: str
    tier: str


PRIVATE_LABEL: tuple[Brand, ...] = tuple(
    Brand(name, "value")
    for name in (
        "No Name",
        "Ritebrand",
        "PnP",
        "Checkers Housebrand",
        "Shoprite",
        "Woolworths",
        "SPAR",
        "Boxer",
    )
)
BRANDS: dict[str, tuple[Brand, ...]] = {
    "dairy": (
        Brand("Clover", "mainstream"),
        Brand("Parmalat", "mainstream"),
        Brand("Douglasdale", "premium"),
        Brand("Danone", "mainstream"),
        Brand("Woodlands", "premium"),
        Brand("Crickley Dairy", "value"),
    ),
    "soft_drink": (
        Brand("Coca-Cola", "mainstream"),
        Brand("Pepsi", "mainstream"),
        Brand("Schweppes", "mainstream"),
        Brand("Sparletta", "value"),
        Brand("Twizza", "value"),
        Brand("Jive", "value"),
    ),
    "staple": (
        Brand("Iwisa", "mainstream"),
        Brand("White Star", "mainstream"),
        Brand("Ace", "value"),
        Brand("Tastic", "mainstream"),
        Brand("Spekko", "mainstream"),
        Brand("Selati", "mainstream"),
    ),
    "bakery": (Brand("Albany", "mainstream"), Brand("Sasko", "mainstream"), Brand("BB", "value")),
    "canned": (
        Brand("Koo", "mainstream"),
        Brand("Rhodes", "mainstream"),
        Brand("All Gold", "mainstream"),
        Brand("Lucky Star", "mainstream"),
    ),
    "cleaning": (
        Brand("Sunlight", "mainstream"),
        Brand("Handy Andy", "mainstream"),
        Brand("Domestos", "mainstream"),
        Brand("Maq", "value"),
    ),
    "personal": (
        Brand("Colgate", "mainstream"),
        Brand("Dove", "premium"),
        Brand("Lux", "mainstream"),
        Brand("Protex", "mainstream"),
    ),
    "pet": (
        Brand("Bobtail", "mainstream"),
        Brand("Husky", "mainstream"),
        Brand("Whiskas", "premium"),
        Brand("Dogmor", "value"),
    ),
    "generic": (
        Brand("House", "value"),
        Brand("Select", "mainstream"),
        Brand("Choice", "value"),
        Brand("Premium Choice", "premium"),
    ),
}


def pool(key: str) -> tuple[Brand, ...]:
    """Return a category brand pool plus private labels."""
    return BRANDS.get(key, BRANDS["generic"]) + PRIVATE_LABEL
