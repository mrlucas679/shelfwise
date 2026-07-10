from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from shelfwise_data import (
    DEFAULT_DATASETS,
    REFERENCE_NOW,
    ProductRow,
    StockRow,
    build_store_intelligence_demo,
    load_products,
    load_stock,
)
from shelfwise_worldgen.catalog.generate import count_estimate, generate_catalog
from shelfwise_worldgen.catalog.model import CatalogProduct

DEFAULT_PRODUCT_LIMIT = 20
MAX_PRODUCT_LIMIT = 50
DEFAULT_SYNTHETIC_SCAN_BUDGET = 2_000
DEFAULT_CATALOG_SEED = 42
DEFAULT_CATALOG_SCALE = "hypermarket"


def product_attention_queue(
    *,
    limit: int = DEFAULT_PRODUCT_LIMIT,
    datasets_dir: Path = DEFAULT_DATASETS,
    now: datetime = REFERENCE_NOW,
) -> dict[str, Any]:
    """Return bounded product groups that need action; never an inventory dump."""

    bounded_limit = _bounded_limit(limit)
    items = [
        item
        for item in _seed_product_items(datasets_dir=datasets_dir, now=now)
        if item["requires_attention"]
    ]
    ordered = _attention_sort(items)
    sell_first = [item for item in ordered if "sell_first" in item["attention_reasons"]]
    to_order = [item for item in ordered if "low_stock" in item["attention_reasons"]]
    expiring = [item for item in ordered if "expiring" in item["attention_reasons"]]

    return {
        "limit": bounded_limit,
        "truncated": len(ordered) > bounded_limit,
        "totals": {
            "attention_products": len(ordered),
            "sell_first_products": len(sell_first),
            "to_order_products": len(to_order),
            "expiring_products": len(expiring),
        },
        "items": ordered[:bounded_limit],
        "sell_first": sell_first[:bounded_limit],
        "to_order": to_order[:bounded_limit],
        "expiring": expiring[:bounded_limit],
    }


def search_product_catalog(
    *,
    query: str = "",
    limit: int = DEFAULT_PRODUCT_LIMIT,
    datasets_dir: Path = DEFAULT_DATASETS,
    now: datetime = REFERENCE_NOW,
    seed: int = DEFAULT_CATALOG_SEED,
    scale: str = DEFAULT_CATALOG_SCALE,
    synthetic_scan_budget: int = DEFAULT_SYNTHETIC_SCAN_BUDGET,
) -> dict[str, Any]:
    """Search a bounded product catalogue with attention products ranked first."""

    bounded_limit = _bounded_limit(limit)
    bounded_scan_budget = _bounded_scan_budget(synthetic_scan_budget)
    clean_query = query.strip()
    if not clean_query:
        attention = product_attention_queue(limit=bounded_limit, datasets_dir=datasets_dir, now=now)
        return {
            "query": "",
            "limit": bounded_limit,
            "truncated": attention["truncated"],
            "products": attention["items"],
            "source_counts": {"seed": len(attention["items"]), "synthetic_catalog": 0},
        }

    terms = _query_terms(clean_query)
    seen: set[str] = set()
    seed_matches = [
        item
        for item in _seed_product_items(datasets_dir=datasets_dir, now=now)
        if _matches_terms(terms, _seed_haystack(item))
    ]
    results: list[dict[str, Any]] = []
    for item in _attention_sort(seed_matches):
        _append_unique(results, seen, item)

    synthetic_scanned = 0
    synthetic_matches = 0
    synthetic_total = count_estimate(scale)
    for product in generate_catalog(seed, scale=scale):
        if synthetic_scanned >= bounded_scan_budget:
            break
        synthetic_scanned += 1
        if not _matches_terms(terms, _catalog_haystack(product)):
            continue
        synthetic_matches += 1
        _append_unique(results, seen, _catalog_item(product))
        if len(results) >= bounded_limit and synthetic_matches >= bounded_limit:
            break

    products = results[:bounded_limit]
    return {
        "query": clean_query,
        "limit": bounded_limit,
        "truncated": len(results) > bounded_limit or synthetic_scanned < synthetic_total,
        "products": products,
        "source_counts": {
            "seed": len(seed_matches),
            "synthetic_catalog": synthetic_matches,
            "synthetic_scanned": synthetic_scanned,
            "synthetic_scan_budget": bounded_scan_budget,
            "synthetic_total_estimate": synthetic_total,
        },
    }


def _bounded_limit(limit: int) -> int:
    if limit <= 0 or limit > MAX_PRODUCT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRODUCT_LIMIT}")
    return limit


def _bounded_scan_budget(limit: int) -> int:
    if limit <= 0:
        raise ValueError("synthetic_scan_budget must be positive")
    return limit


def _seed_product_items(*, datasets_dir: Path, now: datetime) -> list[dict[str, Any]]:
    products = {
        product.sku: product
        for product in load_products(Path(datasets_dir) / "products.csv")
    }
    stock_rows = load_stock(Path(datasets_dir) / "stock.csv")
    fefo_by_sku = _fefo_by_sku()
    items: list[dict[str, Any]] = []
    for stock in stock_rows:
        product = products.get(stock.sku)
        if product is None:
            continue
        items.append(_seed_item(product, stock, now=now, fefo=fefo_by_sku.get(stock.sku)))
    return items


def _seed_item(
    product: ProductRow,
    stock: StockRow,
    *,
    now: datetime,
    fefo: dict[str, Any] | None,
) -> dict[str, Any]:
    days_to_expiry = max(0, (stock.expiry_date - now.date()).days)
    low_stock = stock.on_hand <= stock.reorder_point
    expiring = days_to_expiry <= 7
    sell_first_units = (
        int(fefo.get("priority_sell_units", 0))
        if fefo
        else (stock.on_hand if days_to_expiry <= 3 else 0)
    )
    normal_units = int(fefo.get("normal_units", 0)) if fefo else 0
    blocked_units = int(fefo.get("blocked_units", 0)) if fefo else 0
    total_units = int(fefo.get("total_units", stock.on_hand)) if fefo else stock.on_hand

    reasons: list[str] = []
    if sell_first_units > 0:
        reasons.append("sell_first")
    if expiring:
        reasons.append("expiring")
    if low_stock:
        reasons.append("low_stock")

    detail_parts = []
    if days_to_expiry <= 0:
        detail_parts.append("expires today")
    else:
        detail_parts.append(f"{days_to_expiry} days to expiry")
    if sell_first_units > 0:
        detail_parts.append(f"{sell_first_units} sell-first units")
    detail_parts.append(f"{stock.on_hand} on hand")
    detail_parts.append(f"reorder at {stock.reorder_point}")

    return {
        "sku": product.sku,
        "product_id": product.sku,
        "name": product.name,
        "category": product.category,
        "supplier": product.supplier,
        "source": "seed_csv",
        "synthetic": False,
        "price": product.price.to_dict(),
        "cost": product.cost.to_dict(),
        "on_hand": stock.on_hand,
        "reorder_point": stock.reorder_point,
        "expiry_date": stock.expiry_date.isoformat(),
        "days_to_expiry": days_to_expiry,
        "requires_attention": bool(reasons),
        "attention_reasons": reasons,
        "attention_summary": " · ".join(detail_parts),
        "sell_first_units": sell_first_units,
        "normal_units": normal_units,
        "blocked_units": blocked_units,
        "total_units": total_units,
        "lot_count": len(fefo.get("fefo_batches", [])) if fefo else 1,
        "fefo_batches": fefo.get("fefo_batches", []) if fefo else [],
    }


def _catalog_item(product: CatalogProduct) -> dict[str, Any]:
    return {
        "sku": product.sku,
        "product_id": product.product_id,
        "barcode": product.barcode,
        "plu": product.plu,
        "name": product.name,
        "receipt_name": product.receipt_name,
        "brand": product.brand,
        "generic_name": product.generic_name,
        "department": product.department,
        "category": product.category,
        "subcategory": product.subcategory,
        "supplier": product.supplier,
        "source": "synthetic_catalog",
        "synthetic": True,
        "price": {"minor_units": product.price_cents, "currency": product.currency},
        "shelf_location": product.shelf_location,
        "storage_requirements": product.storage_requirements,
        "requires_attention": False,
        "attention_reasons": [],
        "attention_summary": "Catalogue match; no active attention signal.",
    }


def _fefo_by_sku() -> dict[str, dict[str, Any]]:
    demo = build_store_intelligence_demo()
    split = demo.get("batch_split") if isinstance(demo, dict) else None
    if not isinstance(split, dict):
        return {}
    sku = str(split.get("sku") or "").strip()
    return {sku: split} if sku else {}


def _attention_sort(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            -int(item.get("sell_first_units") or 0),
            int(item.get("days_to_expiry") or 9999),
            str(item.get("name") or ""),
        ),
    )


def _append_unique(
    results: list[dict[str, Any]],
    seen: set[str],
    item: dict[str, Any],
) -> None:
    key = str(item.get("sku") or item.get("product_id") or "").lower()
    if not key or key in seen:
        return
    seen.add(key)
    results.append(item)


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(part for part in query.lower().split() if part)


def _matches_terms(terms: tuple[str, ...], haystack: str) -> bool:
    return bool(terms) and all(term in haystack for term in terms)


def _seed_haystack(item: dict[str, Any]) -> str:
    values = [
        item.get("sku"),
        item.get("name"),
        item.get("category"),
        item.get("supplier"),
        item.get("attention_summary"),
    ]
    return _haystack(values)


def _catalog_haystack(product: CatalogProduct) -> str:
    return _haystack(
        [
            product.product_id,
            product.barcode,
            product.plu,
            product.name,
            product.receipt_name,
            product.brand,
            product.generic_name,
            product.department,
            product.category,
            product.subcategory,
            product.supplier,
        ]
    )


def _haystack(values: Iterable[object]) -> str:
    return " ".join(str(value).lower() for value in values if value)
