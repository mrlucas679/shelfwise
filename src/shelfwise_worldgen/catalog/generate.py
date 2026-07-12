from __future__ import annotations

import zlib
from collections.abc import Iterator
from dataclasses import replace
from random import Random

from .brands import pool
from .grammar import PACKS, receipt_name
from .gs1 import make_ean13, make_plu
from .model import CatalogProduct
from .physics import PHYSICS
from .taxonomy import iter_subcats

VAT_STANDARD = 0.15
SCALE = {"convenience": (3, 1, 3), "supermarket": (6, 3, 5), "hypermarket": (12, 5, 8)}
FLEET_SKU_TARGET = 500_000
_TIER_FACTOR = {"value": 0.82, "mainstream": 1.0, "premium": 1.38}
_FLAVOURS = ("Original", "Lite", "Low Fat", "Family", "Value", "Extra", "Choice", "Classic", "Rich")


def generate_catalog(seed: int, *, scale: str = "supermarket") -> Iterator[CatalogProduct]:
    """Stream deterministic synthetic products expanded from the taxonomy."""
    if scale == "fleet":
        yield from _generate_fleet_catalog(seed)
        return
    if scale not in SCALE:
        raise ValueError(f"unknown catalog scale: {scale}")
    n_brand, n_variant, n_pack = SCALE[scale]
    counter = 0
    for department, category, subcat in iter_subcats():
        rng = Random(_seed_int(seed, department.name, category.name, subcat.name))
        brands = list(pool(subcat.brand_pool, seed))
        rng.shuffle(brands)
        variants = rng.sample(_FLAVOURS, k=min(n_variant, len(_FLAVOURS)))
        loose = subcat.pack == "produce_wt"
        for brand in brands[:n_brand]:
            for variant in variants:
                for size_label, unit, factor in PACKS[subcat.pack][:n_pack]:
                    counter += 1
                    name = f"{brand.name} {subcat.name} {variant}".replace(" Original", "")
                    price = _psych(int(subcat.base_c * factor * _TIER_FACTOR[brand.tier]))
                    yield CatalogProduct(
                        product_id=f"P{counter:08d}",
                        barcode=None if loose else make_ean13(counter),
                        plu=make_plu(counter, organic="Organic" in subcat.name) if loose else None,
                        name=name,
                        receipt_name=receipt_name(name, size_label),
                        brand=brand.name,
                        generic_name=subcat.name,
                        department=department.name,
                        category=category.name,
                        subcategory=subcat.name,
                        physics=subcat.physics,
                        size_label=size_label,
                        unit=unit,
                        price_cents=price,
                        vat_rate=0.0 if subcat.vat == "zero" else VAT_STANDARD,
                        supplier=f"{brand.name} SA (Pty) Ltd",
                        shelf_location=f"{department.aisle}-{(counter % 40) + 1:02d}",
                        organic="Organic" in subcat.name,
                        vegan=subcat.vegan,
                        gluten_free=subcat.gluten_free,
                        halal=rng.random() < 0.6 and subcat.physics not in {"meat", "poultry"},
                        allergens=subcat.allergens,
                        storage_requirements=_storage(subcat.physics),
                    )


def count_estimate(seed: int, scale: str = "supermarket") -> int:
    """Estimate how many products a scale profile will generate."""
    if scale == "fleet":
        return FLEET_SKU_TARGET
    if scale not in SCALE:
        raise ValueError(f"unknown catalog scale: {scale}")
    n_brand, n_variant, n_pack = SCALE[scale]
    total = 0
    for _department, _category, subcat in iter_subcats():
        total += (
            min(n_brand, len(pool(subcat.brand_pool, seed)))
            * n_variant
            * min(n_pack, len(PACKS[subcat.pack]))
        )
    return total


def _generate_fleet_catalog(seed: int) -> Iterator[CatalogProduct]:
    """Repeat the rich hypermarket base deterministically to a 500k-SKU fleet asset.

    The iterator intentionally retains only the 15k-product base catalogue. Consumers can
    stream the remaining records to bulk import or scoring jobs without a 500k in-memory list.
    """
    base_catalog = tuple(generate_catalog(seed, scale="hypermarket"))
    for sequence in range(1, FLEET_SKU_TARGET + 1):
        template = base_catalog[(sequence - 1) % len(base_catalog)]
        range_number = (sequence - 1) // len(base_catalog) + 1
        name = f"{template.name} Range {range_number}"
        plu = (
            None
            if template.plu is None
            else make_plu(sequence, organic=template.plu.startswith("9"))
        )
        yield replace(
            template,
            product_id=f"P{sequence:08d}",
            barcode=None if template.barcode is None else make_ean13(sequence),
            plu=plu,
            name=name,
            receipt_name=receipt_name(name, template.size_label),
            shelf_location=f"{template.department[:1].upper()}-{(sequence % 40) + 1:02d}",
        )


def _seed_int(*parts: object) -> int:
    """Create a stable seed that is not affected by Python hash randomization."""
    return zlib.crc32("|".join(str(part) for part in parts).encode())


def _psych(cents: int) -> int:
    """Apply common shelf pricing ending in 99 cents."""
    return max(99, round(cents / 100) * 100 - 1)


def _storage(physics: str) -> str:
    """Convert a physics class into a readable storage instruction."""
    storage = PHYSICS[physics].storage
    if storage == "frozen":
        return "Keep frozen at or below -18 degrees C"
    if storage == "chilled":
        return "Keep refrigerated at or below 4 degrees C"
    return "Store in a cool dry place"
