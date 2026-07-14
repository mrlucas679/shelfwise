from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .candidate_factory import generate_fleet_candidates
from .product_policies import resolve_product_policy
from .retail_intelligence import DeliveryReceipt, reconcile_delivery

DEFAULT_PRODUCT_LIMIT = 20
MAX_PRODUCT_LIMIT = 50


def product_attention_queue(
    *,
    facts: Any,
    tenant_id: str,
    limit: int = DEFAULT_PRODUCT_LIMIT,
    now: datetime | None = None,
    candidate_store: Any | None = None,
    open_orders: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return bounded product groups that need action; never an inventory dump."""

    bounded_limit = _bounded_limit(limit)
    items = [
        item
        for item in _provider_product_items(facts, tenant_id=tenant_id, now=now)
        if item["requires_attention"]
    ]
    ordered = _attention_sort(items)
    sell_first = [item for item in ordered if "sell_first" in item["attention_reasons"]]
    to_order = [item for item in ordered if "low_stock" in item["attention_reasons"]]
    expiring = [item for item in ordered if "expiring" in item["attention_reasons"]]
    deliveries = [_delivery_exception(item) for item in to_order]
    candidates = generate_fleet_candidates(
        ordered,
        tenant_id=tenant_id,
        as_of=(now or datetime.now(UTC)).date(),
        limit=bounded_limit,
        open_orders=open_orders,
    )

    candidate_records = (
        candidate_store.upsert_many(candidates, now=now)
        if candidate_store is not None
        else [candidate.to_dict() for candidate in candidates]
    )
    if candidate_store is not None and open_orders:
        candidate_records = _suppress_covered_candidates(
            candidate_records,
            candidate_store=candidate_store,
            open_orders=open_orders,
            now=now or datetime.now(UTC),
            tenant_id=tenant_id,
        )

    source = _facts_source(facts)
    return {
        "data_domain": _facts_domain(facts),
        "source_counts": {source: len(ordered)},
        "limit": bounded_limit,
        "truncated": len(ordered) > bounded_limit,
        "totals": {
            "attention_products": len(ordered),
            "sell_first_products": len(sell_first),
            "to_order_products": len(to_order),
            "expiring_products": len(expiring),
            "delivery_issues": sum(1 for d in deliveries if d["missing_units"] > 0),
            "candidates": len(candidates),
        },
        "items": ordered[:bounded_limit],
        "sell_first": sell_first[:bounded_limit],
        "to_order": to_order[:bounded_limit],
        "expiring": expiring[:bounded_limit],
        "deliveries": deliveries[:bounded_limit],
        "candidates": candidate_records,
    }


def get_delivery_exception(
    *, facts: Any, tenant_id: str, sku: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The individual delivery reconciliation for one SKU, by real name - not just a code."""
    for item in _provider_product_items(facts, tenant_id=tenant_id, now=now):
        if item["sku"] == sku:
            return _delivery_exception(item) if "low_stock" in item["attention_reasons"] else None
    return None


def _delivery_exception(item: dict[str, Any]) -> dict[str, Any]:
    """A real, per-product delivery reconciliation - every under-stocked product gets its own
    receiving record instead of the whole store sharing one hero SKU's delivery status."""
    reorder_point = int(item["reorder_point"])
    ordered_units = max(reorder_point * 3, 1)
    received_units = max(0, ordered_units - reorder_point)
    reconciliation = reconcile_delivery(
        DeliveryReceipt(
            sku=item["sku"],
            ordered_units=ordered_units,
            asn_units=ordered_units,
            received_units=received_units,
            accepted_units=received_units,
            short_dated_units=0,
        )
    )
    payload = reconciliation.to_dict()
    payload["product_name"] = item["name"]
    payload["category"] = item["category"]
    payload["supplier"] = item["supplier"]
    return payload


def _suppress_covered_candidates(
    records: list[dict[str, Any]],
    *,
    candidate_store: Any,
    open_orders: dict[str, dict[str, Any]],
    now: datetime,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Suppress reorder noise when an open order already covers the stock gap."""
    updated: list[dict[str, Any]] = []
    for record in records:
        if record.get("candidate_type") != "low_stock":
            updated.append(record)
            continue
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        gap = max(
            int(evidence.get("reorder_point") or 0) - int(record.get("exposure_units") or 0),
            0,
        )
        coverage = open_orders.get(str(record.get("sku"))) or {}
        remaining = int(coverage.get("remaining_units") or 0)
        if gap <= 0 or remaining < gap:
            updated.append(record)
            continue
        eta = _parse_eta(coverage.get("eta"), fallback=now + timedelta(days=1))
        suppressed = candidate_store.suppress(
            tenant_id,
            str(record["candidate_key"]),
            reason=f"open order covers {remaining} units; reorder gap is {gap}",
            until=eta,
        )
        updated.append(suppressed or record)
    return updated


def _parse_eta(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def search_product_catalog(
    *,
    facts: Any,
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
            "source_counts": attention["source_counts"],
            "data_domain": attention["data_domain"],
        }

    terms = _query_terms(clean_query)
    all_items = _provider_product_items(facts, tenant_id=tenant_id, now=now)
    matches = [item for item in all_items if _matches_terms(terms, _item_haystack(item))]
    ordered = _attention_sort(matches)
    products = ordered[:bounded_limit]
    source = _facts_source(facts)
    return {
        "query": clean_query,
        "limit": bounded_limit,
        "truncated": len(ordered) > bounded_limit,
        "products": products,
        "source_counts": {source: len(matches)},
        "data_domain": _facts_domain(facts),
    }


def _bounded_limit(limit: int) -> int:
    if limit <= 0 or limit > MAX_PRODUCT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRODUCT_LIMIT}")
    return limit


def _provider_product_items(
    facts: Any, *, tenant_id: str, now: datetime | None
) -> list[dict[str, Any]]:
    as_of = (now or datetime.now(UTC)).date()
    stock_by_sku = {row["sku"]: row for row in facts.list_stock(tenant_id)}
    signals_by_sku = facts.list_product_operational_signals(tenant_id)
    source = _facts_source(facts)
    items: list[dict[str, Any]] = []
    for product in facts.list_products(tenant_id):
        stock = stock_by_sku.get(product["sku"])
        if stock is None or not _complete_product_row(product, stock):
            continue
        signals = signals_by_sku.get(product["sku"], {})
        supplier = signals.get("supplier")
        recent_daily_units = signals.get("recent_daily_units", ())
        items.append(
            _product_item(
                product,
                stock,
                as_of=as_of,
                supplier=supplier,
                recent_daily_units=recent_daily_units,
                source=source,
            )
        )
    return items


def _product_item(
    product: dict[str, Any],
    stock: dict[str, Any],
    *,
    as_of: date,
    supplier: dict[str, Any] | None = None,
    recent_daily_units: Iterable[Any] = (),
    source: str = "generated_world",
) -> dict[str, Any]:
    batches = _batch_items(stock, as_of=as_of)
    policy = resolve_product_policy(product.get("category"), product.get("physics"))
    expiry_date = min((item["expiry_date"] for item in batches), default=stock["expiry_date"])
    days_to_expiry = min((item["days_to_expiry"] for item in batches), default=0)
    on_hand = int(stock["on_hand"])
    reorder_point = int(stock["reorder_point"])
    low_stock = on_hand <= reorder_point
    expiring = days_to_expiry <= policy.expiry_review_days
    recent_units = [int(float(value or 0)) for value in recent_daily_units]
    average_daily_units = (
        sum(recent_units) / len(recent_units) if recent_units else 0.0
    )
    supplier_delay = bool((supplier or {}).get("recent_delay"))
    slow_mover = bool(
        recent_units and average_daily_units <= 1 and on_hand > reorder_point
    )
    overstock = bool(
        recent_units
        and average_daily_units > 0
        and on_hand / average_daily_units >= 30
        and on_hand > reorder_point
    )
    missing_batch_expiry = not (
        isinstance(stock.get("batches"), list) and stock.get("batches")
    )
    blocked_units = sum(
        int(item["on_hand"]) for item in batches if int(item["days_to_expiry"]) < 0
    )
    sell_first_units = sum(
        int(item["on_hand"])
        for item in batches
        if 0 <= int(item["days_to_expiry"]) <= 3
    )
    normal_units = max(on_hand - sell_first_units - blocked_units, 0)

    reasons: list[str] = []
    if sell_first_units > 0:
        reasons.append("sell_first")
    if expiring:
        reasons.append("expiring")
    if blocked_units > 0:
        reasons.append("blocked")
    if low_stock:
        reasons.append("low_stock")
    if supplier_delay:
        reasons.append("supplier_delay")
    if slow_mover:
        reasons.append("slow_mover")
    elif overstock:
        reasons.append("overstock")
    if missing_batch_expiry:
        reasons.append("missing_batch_expiry")

    detail_parts = []
    if days_to_expiry < 0:
        detail_parts.append(f"expired {abs(days_to_expiry)} days ago")
    elif days_to_expiry == 0:
        detail_parts.append("expires today")
    else:
        detail_parts.append(f"{days_to_expiry} days to expiry")
    if sell_first_units > 0:
        detail_parts.append(f"{sell_first_units} sell-first units")
    if blocked_units > 0:
        detail_parts.append(f"{blocked_units} blocked units")
    detail_parts.append(f"{on_hand} on hand")
    detail_parts.append(f"reorder at {reorder_point}")

    return {
        "sku": product["sku"],
        "product_id": product.get("product_id") or product["sku"],
        "name": product["name"],
        "category": product["category"],
        "supplier": product["supplier"],
        "physics": product.get("physics"),
        "recent_daily_units": recent_units,
        "supplier_recent_delay": supplier_delay,
        "supplier_lead_time_days": (supplier or {}).get("lead_time_days"),
        "supplier_available_units": int((supplier or {}).get("available_units") or 0),
        "has_batch_evidence": not missing_batch_expiry,
        "policy": {
            "id": policy.policy_id,
            "expiry_review_days": policy.expiry_review_days,
            "minimum_margin_pct": policy.minimum_margin_pct,
            "cold_chain_sensitive": policy.cold_chain_sensitive,
            "hitl_required": policy.hitl_required,
        },
        "source": source,
        "data_domain": (
            "world_simulation" if source == "generated_world" else "operational_twin"
        ),
        "synthetic": source == "generated_world",
        "unit_price": product["unit_price"],
        "unit_cost": product["unit_cost"],
        "on_hand": on_hand,
        "reorder_point": reorder_point,
        "expiry_date": expiry_date,
        "days_to_expiry": days_to_expiry,
        "requires_attention": bool(reasons),
        "attention_reasons": reasons,
        "attention_summary": " · ".join(detail_parts),
        "sell_first_units": sell_first_units,
        "normal_units": normal_units,
        "blocked_units": blocked_units,
        "total_units": on_hand,
        "lot_count": len(batches),
        "batches": batches,
    }


def _batch_items(stock: dict[str, Any], *, as_of: date) -> list[dict[str, Any]]:
    """Return bounded lot-level evidence, with a compatibility row for old snapshots."""
    raw_batches = stock.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raw_batches = [
            {
                "lot_id": f"LOT-{stock['sku']}",
                "on_hand": stock["on_hand"],
                "expiry_date": stock["expiry_date"],
                "received_date": stock.get("received_date"),
            }
        ]
    batches: list[dict[str, Any]] = []
    for raw in raw_batches:
        if not isinstance(raw, dict):
            continue
        expiry = date.fromisoformat(str(raw["expiry_date"]))
        batches.append(
            {
                "lot_id": str(raw.get("lot_id") or raw.get("lot") or "unknown"),
                "on_hand": int(raw.get("on_hand") or 0),
                "expiry_date": expiry.isoformat(),
                "received_date": raw.get("received_date"),
                "days_to_expiry": (expiry - as_of).days,
            }
        )
    return batches


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


def _facts_source(facts: Any) -> str:
    return str(getattr(facts, "source_dataset", "generated_world"))


def _facts_domain(facts: Any) -> str:
    return str(getattr(facts, "data_domain", "world_simulation"))


def _complete_product_row(product: dict[str, Any], stock: dict[str, Any]) -> bool:
    product_fields = ("sku", "name", "category", "supplier", "unit_price", "unit_cost")
    stock_fields = ("on_hand", "reorder_point", "expiry_date")
    return all(product.get(field) is not None for field in product_fields) and all(
        stock.get(field) is not None for field in stock_fields
    )
