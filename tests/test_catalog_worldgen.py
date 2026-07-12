from __future__ import annotations

from shelfwise_worldgen.catalog.generate import count_estimate, generate_catalog
from shelfwise_worldgen.catalog.grammar import PACKS
from shelfwise_worldgen.catalog.gs1 import ean13_check_digit, is_valid_ean13, make_ean13, make_plu
from shelfwise_worldgen.catalog.physics import PHYSICS
from shelfwise_worldgen.catalog.sample import generate_receipts, sample_assortment
from shelfwise_worldgen.catalog.taxonomy import TAXONOMY, iter_subcats


def _first(seed: int, count: int, **kwargs):
    """Collect a bounded prefix from the streaming generator."""
    output = []
    for product in generate_catalog(seed, **kwargs):
        output.append(product)
        if len(output) >= count:
            break
    return output


def test_barcodes_have_valid_check_digit_in_restricted_range():
    for product in _first(1, 200):
        if product.barcode is not None:
            assert is_valid_ean13(product.barcode)
            assert product.barcode[0] == "2"
    assert make_ean13(123)[-1] == str(ean13_check_digit(make_ean13(123)[:12]))
    assert make_plu(5, organic=True).startswith("9")


def test_sa_vat_zero_rating_is_honored():
    by_subcat = {product.subcategory: product for product in _first(7, 4_000)}
    for basic in ("Maize Meal", "Brown Bread", "Fresh Milk", "Eggs", "Rice", "Tinned Pilchards"):
        assert by_subcat[basic].vat_rate == 0.0
    assert by_subcat["Cola"].vat_rate == 0.15
    assert by_subcat["Washing Powder"].vat_rate == 0.15


def test_generation_is_deterministic_and_labeled():
    first = [
        (product.product_id, product.barcode, product.price_cents)
        for product in _first(3, 300)
    ]
    second = [
        (product.product_id, product.barcode, product.price_cents)
        for product in _first(3, 300)
    ]
    assert first == second
    assert all(product.synthetic for product in _first(3, 50))


def test_barcodes_are_unique():
    codes = [product.barcode for product in _first(9, 1_000) if product.barcode]
    assert len(codes) == len(set(codes))


def test_receipt_name_is_uppercase_bounded_nonempty():
    for product in _first(2, 200):
        assert product.receipt_name
        assert product.receipt_name == product.receipt_name.upper()
        assert len(product.receipt_name) <= 22


def test_taxonomy_integrity_and_breadth():
    assert len(TAXONOMY) >= 45
    for _department, _category, subcat in iter_subcats():
        assert subcat.physics in PHYSICS
        assert subcat.pack in PACKS
        assert subcat.base_c > 0


def test_scale_profiles_grow_monotonically():
    assert count_estimate(1, "convenience") < count_estimate(1, "supermarket")
    assert count_estimate(1, "supermarket") < count_estimate(1, "hypermarket")
    assert count_estimate(1, "hypermarket") > 5_000


def test_assortment_spans_departments_and_is_deterministic():
    assortment = sample_assortment(5, size=400)
    assert len(assortment) == 400
    assert len({product.department for product in assortment}) >= 8
    assert [product.product_id for product in assortment] == [
        product.product_id for product in sample_assortment(5, size=400)
    ]


def test_receipts_reconcile_and_respect_zero_rating():
    assortment = sample_assortment(5, size=400)
    for receipt in generate_receipts(11, n=50, assortment=assortment):
        assert receipt.subtotal_c == sum(line.line_total_c for line in receipt.lines)
        assert receipt.vat_total_c == sum(line.vat_c for line in receipt.lines)
        assert receipt.total_c == receipt.subtotal_c
        for line in receipt.lines:
            assert line.vat_c <= round(line.line_total_c * 0.15)
