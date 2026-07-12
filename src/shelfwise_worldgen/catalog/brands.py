from __future__ import annotations

from dataclasses import dataclass
from random import Random


@dataclass(frozen=True, slots=True)
class Brand:
    name: str
    tier: str


# Chain-neutral value names any single store can plausibly stock as its own house brand.
GENERIC_PRIVATE_LABEL: tuple[Brand, ...] = tuple(
    Brand(name, "value") for name in ("No Name", "Ritebrand")
)
# Real SA retail-chain private labels are mutually exclusive per store: a Pick n Pay does not
# also stock Woolworths-, Shoprite-, SPAR-, Boxer-, and Checkers-branded private label lines on
# the same shelf. Exactly one is picked deterministically per world seed in `house_brand_name`
# and used as this tenant's single in-house label, instead of blending every competitor's brand
# into one store's catalogue (a real data-quality bug: it read as "products from different
# shops").
_CHAIN_PRIVATE_LABEL_NAMES: tuple[str, ...] = (
    "PnP",
    "Checkers Housebrand",
    "Shoprite",
    "Woolworths",
    "SPAR",
    "Boxer",
)


def house_brand_name(seed: int) -> str:
    """Pick this world's single in-house retail-chain label, deterministically from the seed."""
    return Random(seed).choice(_CHAIN_PRIVATE_LABEL_NAMES)


def private_label_pool(seed: int) -> tuple[Brand, ...]:
    """This tenant's private-label pool: chain-neutral names plus its one house brand."""
    return (*GENERIC_PRIVATE_LABEL, Brand(house_brand_name(seed), "value"))
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


def pool(key: str, seed: int) -> tuple[Brand, ...]:
    """Return a category brand pool plus this world's own private labels."""
    return BRANDS.get(key, BRANDS["generic"]) + private_label_pool(seed)
