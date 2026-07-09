from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .seed import (
    DEFAULT_DATASETS,
    REFERENCE_NOW,
    load_products,
    load_sales,
    load_stock,
)


def build_context(
    datasets_dir: Path = DEFAULT_DATASETS,
    *,
    now: datetime = REFERENCE_NOW,
) -> dict[str, dict[str, Any]]:
    """Build the Money-typed SKU context used by backend reasoning and memory."""

    datasets_dir = Path(datasets_dir)
    products = {product.sku: product for product in load_products(datasets_dir / "products.csv")}
    daily = _base_daily_sales(datasets_dir)
    context: dict[str, dict[str, Any]] = {}
    for stock in load_stock(datasets_dir / "stock.csv"):
        product = products.get(stock.sku)
        if product is None:
            continue
        context[stock.sku] = {
            "on_hand": stock.on_hand,
            "days_to_expiry": max(0, (stock.expiry_date - now.date()).days),
            "base_daily_sales": daily.get(stock.sku, Decimal("0")),
            "price": product.price,
            "cost": product.cost,
            "reorder_point": stock.reorder_point,
            "category": product.category.strip().lower(),
            "area": stock.location,
            "supplier": product.supplier,
        }
    return context


def build_thresholds(datasets_dir: Path = DEFAULT_DATASETS) -> dict[str, int]:
    """Return SKU reorder thresholds from the CSV stock export."""

    return {
        stock.sku: stock.reorder_point
        for stock in load_stock(Path(datasets_dir) / "stock.csv")
    }


def _base_daily_sales(datasets_dir: Path) -> dict[str, Decimal]:
    totals: dict[str, dict[object, int]] = defaultdict(lambda: defaultdict(int))
    for sale in load_sales(datasets_dir / "sales.csv"):
        totals[sale.sku][sale.ts.date()] += sale.quantity

    daily: dict[str, Decimal] = {}
    for sku, by_day in totals.items():
        daily[sku] = Decimal(sum(by_day.values())) / Decimal(len(by_day))
    return daily
