from __future__ import annotations

import pytest

from shelfwise_catalog import (
    ConflictingIdentifierError,
    InMemoryProductCatalogStore,
    Product,
    ProductIdentifier,
    ProductVariant,
)


def _seed_full_cream_milk_1l(store: InMemoryProductCatalogStore, *, tenant_id: str = "t1") -> None:
    store.upsert_product(
        Product(tenant_id=tenant_id, product_id="prod_milk_full_cream", name="Full Cream Milk")
    )
    store.upsert_variant(
        ProductVariant(
            tenant_id=tenant_id,
            variant_id="var_milk_full_cream_1l",
            product_id="prod_milk_full_cream",
            pack_size="1L",
            unit_of_measure="each",
        )
    )


def test_two_source_systems_resolve_the_same_variant_through_different_identifiers() -> None:
    """The core identity-resolution proof: SAP's material code and the POS barcode for the
    SAME physical item must resolve to the SAME variant, not two different "products"."""
    store = InMemoryProductCatalogStore()
    _seed_full_cream_milk_1l(store)
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1",
            variant_id="var_milk_full_cream_1l",
            kind="source_system_id",
            value="4011",
            source_system="sap",
        )
    )
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1",
            variant_id="var_milk_full_cream_1l",
            kind="barcode",
            value="6001234567890",
            source_system="pos",
        )
    )

    from_sap = store.resolve_identifier(tenant_id="t1", kind="source_system_id", value="4011")
    from_pos = store.resolve_identifier(tenant_id="t1", kind="barcode", value="6001234567890")

    assert from_sap is not None
    assert from_pos is not None
    assert from_sap["variant_id"] == from_pos["variant_id"] == "var_milk_full_cream_1l"


def test_resolve_identifier_returns_none_for_an_unknown_code() -> None:
    store = InMemoryProductCatalogStore()

    assert store.resolve_identifier(tenant_id="t1", kind="gtin", value="does-not-exist") is None


def test_remapping_an_identifier_to_a_different_variant_is_rejected_not_silently_overwritten() -> (
    None
):
    """A conflicting remap needs explicit human review, not a silent overwrite that could
    merge two unrelated products under one identity."""
    store = InMemoryProductCatalogStore()
    _seed_full_cream_milk_1l(store)
    store.upsert_variant(
        ProductVariant(tenant_id="t1", variant_id="var_other", product_id="prod_other")
    )
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1",
            variant_id="var_milk_full_cream_1l",
            kind="gtin",
            value="6001234567890",
        )
    )

    with pytest.raises(ConflictingIdentifierError, match="already mapped to variant"):
        store.upsert_identifier(
            ProductIdentifier(
                tenant_id="t1", variant_id="var_other", kind="gtin", value="6001234567890"
            )
        )

    # Re-asserting the SAME mapping again (idempotent retry) must not raise.
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1",
            variant_id="var_milk_full_cream_1l",
            kind="gtin",
            value="6001234567890",
        )
    )


def test_identifiers_and_variants_are_isolated_per_tenant() -> None:
    store = InMemoryProductCatalogStore()
    _seed_full_cream_milk_1l(store, tenant_id="tenant_a")
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="tenant_a",
            variant_id="var_milk_full_cream_1l",
            kind="sku",
            value="MILK-1L",
        )
    )

    assert store.resolve_identifier(tenant_id="tenant_b", kind="sku", value="MILK-1L") is None
    assert store.list_products(tenant_id="tenant_b") == []
    assert len(store.list_products(tenant_id="tenant_a")) == 1


def test_list_variants_filters_by_product_and_identifiers_list_by_variant() -> None:
    store = InMemoryProductCatalogStore()
    _seed_full_cream_milk_1l(store)
    store.upsert_variant(
        ProductVariant(
            tenant_id="t1",
            variant_id="var_milk_full_cream_2l",
            product_id="prod_milk_full_cream",
            pack_size="2L",
        )
    )
    store.upsert_variant(
        ProductVariant(tenant_id="t1", variant_id="var_unrelated", product_id="prod_other")
    )
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1", variant_id="var_milk_full_cream_1l", kind="sku", value="MILK-1L"
        )
    )
    store.upsert_identifier(
        ProductIdentifier(
            tenant_id="t1", variant_id="var_milk_full_cream_1l", kind="plu", value="4011"
        )
    )

    variants = store.list_variants(tenant_id="t1", product_id="prod_milk_full_cream")
    identifiers = store.list_identifiers(tenant_id="t1", variant_id="var_milk_full_cream_1l")

    assert {item["variant_id"] for item in variants} == {
        "var_milk_full_cream_1l",
        "var_milk_full_cream_2l",
    }
    assert {item["kind"] for item in identifiers} == {"sku", "plu"}


def test_invalid_identifier_kind_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        ProductIdentifier(tenant_id="t1", variant_id="v1", kind="not_a_real_kind", value="x")
