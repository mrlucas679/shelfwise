from __future__ import annotations

from dataclasses import dataclass

from .physics import PHYSICS, Physics


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    product_id: str
    barcode: str | None
    plu: str | None
    name: str
    receipt_name: str
    brand: str
    generic_name: str
    department: str
    category: str
    subcategory: str
    physics: str
    size_label: str
    unit: str
    price_cents: int
    vat_rate: float
    supplier: str
    shelf_location: str
    currency: str = "ZAR"
    country_of_origin: str = "South Africa"
    organic: bool = False
    vegan: bool = False
    gluten_free: bool = False
    halal: bool = False
    kosher: bool = False
    allergens: tuple[str, ...] = ()
    storage_requirements: str = "Store in a cool dry place"
    synthetic: bool = True

    @property
    def cat(self) -> Physics:
        return PHYSICS[self.physics]

    @property
    def sku(self) -> str:
        return self.product_id

    @property
    def price_low_c(self) -> int:
        return int(self.price_cents * 0.9)

    @property
    def price_high_c(self) -> int:
        return int(self.price_cents * 1.1)
