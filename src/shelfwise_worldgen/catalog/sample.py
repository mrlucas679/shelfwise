from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from random import Random

from .generate import generate_catalog
from .model import CatalogProduct


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    receipt_name: str
    barcode: str | None
    qty: int
    unit_price_c: int
    line_total_c: int
    vat_c: int


@dataclass(frozen=True, slots=True)
class Receipt:
    receipt_id: str
    store: str
    ts: str
    till: int
    lines: list[ReceiptLine]
    subtotal_c: int
    vat_total_c: int
    total_c: int
    synthetic: bool = True


def sample_assortment(
    seed: int,
    *,
    size: int = 1500,
    scale: str = "supermarket",
) -> list[CatalogProduct]:
    """Sample a deterministic store range from a bounded shuffled catalog draw."""
    rng = Random(seed)
    # The fleet profile is intentionally a 500k streaming asset. A store simulator
    # samples a bounded prefix instead of turning that asset into a process-wide list.
    products = (
        islice(generate_catalog(seed, scale=scale), size * 6)
        if scale == "fleet"
        else generate_catalog(seed, scale=scale)
    )
    catalog = list(products)
    rng.shuffle(catalog)
    return catalog[:size]


def generate_receipts(
    seed: int,
    *,
    n: int,
    assortment: list[CatalogProduct],
    store: str = "store_obs_main",
) -> Iterator[Receipt]:
    """Generate VAT-inclusive receipts whose line totals reconcile."""
    rng = Random(seed)
    weights = [1.0 / (index + 1) for index in range(len(assortment))]
    for receipt_index in range(n):
        picks = rng.choices(assortment, weights=weights, k=rng.randint(1, 12))
        lines: list[ReceiptLine] = []
        subtotal = 0
        vat = 0
        for product in picks:
            qty = rng.randint(1, 3)
            line_total = product.price_cents * qty
            line_vat = _line_vat(line_total, product.vat_rate)
            lines.append(
                ReceiptLine(
                    product.receipt_name,
                    product.barcode,
                    qty,
                    product.price_cents,
                    line_total,
                    line_vat,
                )
            )
            subtotal += line_total
            vat += line_vat
        yield Receipt(
            receipt_id=f"R{seed}{receipt_index:06d}",
            store=store,
            ts=(
                f"2026-06-{(receipt_index % 28) + 1:02d}T"
                f"{(receipt_index % 12) + 8:02d}:{receipt_index % 60:02d}:00+00:00"
            ),
            till=(receipt_index % 8) + 1,
            lines=lines,
            subtotal_c=subtotal,
            vat_total_c=vat,
            total_c=subtotal,
        )


def _line_vat(line_total_c: int, vat_rate: float) -> int:
    """Calculate the VAT portion of an inclusive South African receipt line."""
    return 0 if vat_rate == 0.0 else round(line_total_c * (vat_rate / (1 + vat_rate)))


def _take(iterator: Iterator[CatalogProduct], count: int) -> Iterator[CatalogProduct]:
    """Take a bounded number of generated products without materializing a full catalog."""
    for index, item in enumerate(iterator):
        if index >= count:
            return
        yield item
