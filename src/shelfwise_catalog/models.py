from __future__ import annotations

from dataclasses import dataclass

IDENTIFIER_KINDS = frozenset({"gtin", "barcode", "sku", "plu", "source_system_id"})


@dataclass(frozen=True, slots=True)
class Product:
    """A primary product family - e.g. "Full Cream Milk" - independent of pack size.

    At catalog scale a product name is not an identity: "milk" spans many distinct
    sellable variants with different margin, expiry, and storage rules. `Product` is the
    stable family root that `ProductVariant` rows hang off; identity resolution (matching
    a messy source-system row to one of these) happens through `ProductIdentifier`.
    """

    tenant_id: str
    product_id: str
    name: str
    category: str | None = None
    brand: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not self.product_id.strip():
            raise ValueError("product_id is required")
        if not self.name.strip():
            raise ValueError("name is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "product_id": self.product_id,
            "name": self.name,
            "category": self.category,
            "brand": self.brand,
        }


@dataclass(frozen=True, slots=True)
class ProductVariant:
    """A sellable unit of a `Product` - e.g. "1L bottle" vs "case of 12 x 1L".

    Pack size, unit of measure, and case-pack status live here (not on `Product`) because
    they change the expiry policy, margin, and storage rules even for the same product
    family - a case pack and a single unit are not interchangeable for markdown or
    reorder math.
    """

    tenant_id: str
    variant_id: str
    product_id: str
    pack_size: str | None = None
    unit_of_measure: str | None = None
    is_case_pack: bool = False

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not self.variant_id.strip():
            raise ValueError("variant_id is required")
        if not self.product_id.strip():
            raise ValueError("product_id is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "variant_id": self.variant_id,
            "product_id": self.product_id,
            "pack_size": self.pack_size,
            "unit_of_measure": self.unit_of_measure,
            "is_case_pack": self.is_case_pack,
        }


@dataclass(frozen=True, slots=True)
class ProductIdentifier:
    """A single stable-identity mapping: (tenant, kind, value) -> variant_id.

    SKU, barcode, GTIN, PLU, and a source system's own item id are not interchangeable -
    the same physical item can carry a different code in SAP, the POS, and a supplier
    invoice. Each code gets its own row here rather than being folded into one field, so
    a variant can be resolved from any of them.
    """

    tenant_id: str
    variant_id: str
    kind: str
    value: str
    source_system: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not self.variant_id.strip():
            raise ValueError("variant_id is required")
        if self.kind not in IDENTIFIER_KINDS:
            raise ValueError(f"kind must be one of {sorted(IDENTIFIER_KINDS)}, got {self.kind!r}")
        if not self.value.strip():
            raise ValueError("value is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "variant_id": self.variant_id,
            "kind": self.kind,
            "value": self.value,
            "source_system": self.source_system,
        }
