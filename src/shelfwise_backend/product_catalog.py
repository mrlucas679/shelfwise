from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from .world_facts import WorldFactsProvider

DEFAULT_PRODUCT_LIMIT = 20
MAX_PRODUCT_LIMIT = 50


def product_attention_queue(
    *,
    facts: WorldFactsProvider,
    tenant_id: str,
    limit: int = DEFAULT_PRODUCT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return bounded product groups that need action; never an inventory dump."""

    bounded_limit = _bounded_limit(limit)
    items = [
        item
        for item in _world_product_items(facts, tenant_id=tenant_id, now=now)
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
    facts: WorldFactsProvider,
    tenant_id: str,
    query: str = "",
    limit: int = DEFAULT_PRODUCT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Search the generated world's product catalogue, attention products ranked first."""

    bounded_limit = _bounded_limit(limit)
    clean_query = query.strip()
    if not clean_query:
        attention = product_attention_queue(
            facts=facts, limit=bounded_limit, tenant_id=tenant_id, now=now
        )
        return {
            "query": "",
            "limit": bounded_limit,
            "truncated": attention["truncated"],
            "products": attention["items"],
            "source_counts": {"generated_world": len(attention["items"])},
        }

    terms = _query_terms(clean_query)
    all_items = _world_product_items(facts, tenant_id=tenant_id, now=now)
    matches = [item for item in all_items if _matches_terms(terms, _item_haystack(item))]
    ordered = _attention_sort(matches)
    products = ordered[:bounded_limit]
    return {
        "query": clean_query,
        "limit": bounded_limit,
        "truncated": len(ordered) > bounded_limit,
        "products": products,
        "source_counts": {"generated_world": len(matches)},
    }


def _bounded_limit(limit: int) -> int:
    if limit <= 0 or limit > MAX_PRODUCT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRODUCT_LIMIT}")
    return limit


def _world_product_items(
    facts: WorldFactsProvider, *, tenant_id: str, now: datetime | None
) -> list[dict[str, Any]]:
    as_of = (now or datetime.now(UTC)).date()
    stock_by_sku = {row["sku"]: row for row in facts.list_stock(tenant_id)}
    items: list[dict[str, Any]] = []
    for product in facts.list_products(tenant_id):
        stock = stock_by_sku.get(product["sku"])
        if stock is None:
            continue
        items.append(_product_item(product, stock, as_of=as_of))
    return items


def _product_item(
    product: dict[str, Any], stock: dict[str, Any], *, as_of: date
) -> dict[str, Any]:
    expiry_date = date.fromisoformat(stock["expiry_date"])
    days_to_expiry = max(0, (expiry_date - as_of).days)
    on_hand = int(stock["on_hand"])
    reorder_point = int(stock["reorder_point"])
    low_stock = on_hand <= reorder_point
    expiring = days_to_expiry <= 7
    sell_first_units = on_hand if days_to_expiry <= 3 else 0
    normal_units = on_hand - sell_first_units

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
    detail_parts.append(f"{on_hand} on hand")
    detail_parts.append(f"reorder at {reorder_point}")

    return {
        "sku": product["sku"],
        "product_id": product["sku"],
        "name": product["name"],
        "category": product["category"],
        "supplier": product["supplier"],
        "source": "generated_world",
        "synthetic": False,
        "unit_price": product["unit_price"],
        "unit_cost": product["unit_cost"],
        "on_hand": on_hand,
        "reorder_point": reorder_point,
        "expiry_date": stock["expiry_date"],
        "days_to_expiry": days_to_expiry,
        "requires_attention": bool(reasons),
        "attention_reasons": reasons,
        "attention_summary": " · ".join(detail_parts),
        "sell_first_units": sell_first_units,
        "normal_units": normal_units,
        "blocked_units": 0,
        "total_units": on_hand,
        "lot_count": 1,
    }


def _attention_sort(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            -int(item.get("sell_first_units") or 0),
            int(item.get("days_to_expiry") or 9999),
            str(item.get("name") or ""),
        ),
    )


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(part for part in query.lower().split() if part)


def _matches_terms(terms: tuple[str, ...], haystack: str) -> bool:
    return bool(terms) and all(term in haystack for term in terms)


def _item_haystack(item: dict[str, Any]) -> str:
    values = [
        item.get("sku"),
        item.get("name"),
        item.get("category"),
        item.get("supplier"),
        item.get("attention_summary"),
    ]
    return _haystack(values)


def _haystack(values: Iterable[object]) -> str:
    return " ".join(str(value).lower() for value in values if value)
