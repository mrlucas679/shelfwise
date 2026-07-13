from __future__ import annotations

from .catalog.sample import sample_assortment
from .world import WorldConfig


def build_memory_seed(cfg: WorldConfig) -> dict[str, dict]:
    """Build product-master memory rows for the simulator catalog."""
    products = tuple(cfg.products or ())
    if not products:
        products = tuple(sample_assortment(cfg.seed, size=24))
    rows: dict[str, dict] = {}
    for product in products:
        category = product.cat
        sku = str(product.sku)
        rows[sku] = {
            "sku": sku,
            "name": getattr(product, "name", getattr(product, "generic_name", sku)),
            "physics": _physics_name(product),
            "storage": category.storage,
            "refrigerated": category.refrigerated,
            "shelf_life_days": category.shelf_life_days,
            "price_low_c": int(product.price_low_c),
            "price_high_c": int(product.price_high_c),
            "synthetic": True,
        }
    return rows


def _physics_name(product: object) -> str:
    """Return the physics key for catalog or ground-truth product rows."""
    if hasattr(product, "physics"):
        return str(product.physics)
    return str(product.cat.name)
