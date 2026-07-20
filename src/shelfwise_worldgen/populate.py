"""Deterministic Postgres population for the generated retail world.

Replaces the tiny hardcoded CSV/literal seed data with a large, deterministic,
constraint-satisfying catalog produced by ``shelfwise_worldgen.catalog``. No SKU is ever
hardcoded as "the near-expiry one" or "the low-stock one" - a policy declares how many of
each condition must exist, and a guarantee pass *selects* which generated SKUs satisfy each
condition, recording the selection transparently in the returned receipt.
"""

from __future__ import annotations

import os
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Any, Protocol

from shelfwise_contracts import Money
from shelfwise_decision_science import simulate_markdown

from .catalog.sample import sample_assortment

REFERENCE_NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


class WorldSnapshotStoreLike(Protocol):
    def save(self, snapshot: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, tenant_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    """Declarative constraints a generated world must satisfy - never which SKU satisfies them."""

    seed: int
    name: str = "demo"
    catalog_scale: str = "supermarket"
    assortment_size: int = 200
    min_near_expiry: int = 2
    min_low_stock: int = 5
    min_delayed_suppliers: int = 2
    min_price_anomalies: int = 2
    site_count: int = 4

    def __post_init__(self) -> None:
        if self.assortment_size <= 0:
            raise ValueError("assortment_size must be positive")
        for field_name in (
            "min_near_expiry",
            "min_low_stock",
            "min_delayed_suppliers",
            "min_price_anomalies",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.site_count < 1:
            raise ValueError("site_count must be at least 1")


DEMO_POLICY = GenerationPolicy(
    seed=20_260_710,
    name="demo",
    catalog_scale="supermarket",
    assortment_size=200,
    min_near_expiry=2,
    min_low_stock=5,
    min_delayed_suppliers=2,
    min_price_anomalies=2,
    site_count=4,
)


@dataclass(frozen=True, slots=True)
class PopulationReceipt:
    tenant_id: str
    seed: int
    policy: str
    product_count: int
    supplier_count: int
    site_count: int
    near_expiry_skus: tuple[str, ...]
    low_stock_skus: tuple[str, ...]
    delayed_supplier_ids: tuple[str, ...]
    price_anomaly_skus: tuple[str, ...]
    hero_sku: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "seed": self.seed,
            "policy": self.policy,
            "product_count": self.product_count,
            "supplier_count": self.supplier_count,
            "site_count": self.site_count,
            "near_expiry_skus": list(self.near_expiry_skus),
            "low_stock_skus": list(self.low_stock_skus),
            "delayed_supplier_ids": list(self.delayed_supplier_ids),
            "price_anomaly_skus": list(self.price_anomaly_skus),
            "hero_sku": self.hero_sku,
        }


def world_mode() -> str:
    """Return the configured world-population mode (the seam IMPLEMENTATION_PLAN promised).

    ``static`` (default): one-time deterministic population, the mode every current
    deployment runs. ``continuous``: reserved for an always-on world-evolution service;
    the continuous DRIVER that exists today is the full-system harness's world rotation
    (`shelfwise_eval.full_system`, `world_cycles`), which repeatedly evolves and replays
    the generated world through the real pipeline. Any other value fails loudly rather
    than silently behaving like static.
    """
    mode = os.getenv("SHELFWISE_WORLD_MODE", "static").strip().lower() or "static"
    if mode not in {"static", "continuous"}:
        raise ValueError(f"unsupported SHELFWISE_WORLD_MODE: {mode}")
    return mode


def populate_world(
    policy: GenerationPolicy,
    *,
    tenant_id: str,
    store: WorldSnapshotStoreLike,
    now: datetime = REFERENCE_NOW,
) -> PopulationReceipt:
    """Generate a deterministic world for one tenant and persist it through ``store``."""
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    if world_mode() == "continuous":
        raise NotImplementedError(
            "SHELFWISE_WORLD_MODE=continuous is reserved: an always-on world-evolution "
            "service is not built. Use the full-system harness's world rotation "
            "(shelfwise_eval.full_system, --world-cycles) for continuous simulation, or "
            "leave the mode static for one-time deterministic population."
        )
    rng = Random(policy.seed)
    products = sample_assortment(
        policy.seed, size=policy.assortment_size, scale=policy.catalog_scale
    )
    if not products:
        raise ValueError("generated assortment is empty - check catalog_scale/assortment_size")

    supplier_names = sorted({product.supplier for product in products})
    suppliers = {name: _build_supplier(rng, name) for name in supplier_names}

    stock_by_sku: dict[str, dict[str, Any]] = {}
    product_rows: list[dict[str, Any]] = []
    sales_rows: list[dict[str, Any]] = []
    for product in products:
        product_rows.append(_product_row(product))
        stock = _stock_row(rng, product, now)
        stock_by_sku[product.sku] = stock
        sales_rows.extend(_sales_rows(rng, product, now))

    near_expiry = _select_near_expiry(rng, stock_by_sku, policy.min_near_expiry, now)
    low_stock = _select_low_stock(
        rng, stock_by_sku, exclude=set(near_expiry), count=policy.min_low_stock
    )
    delayed_suppliers = _select_delayed_suppliers(rng, suppliers, policy.min_delayed_suppliers)
    price_anomalies = _select_price_anomalies(
        rng,
        product_rows,
        exclude=set(near_expiry) | set(low_stock),
        count=policy.min_price_anomalies,
    )

    primary_location = f"store_{rng.randint(1, 99):02d}"
    for row in stock_by_sku.values():
        row["location"] = primary_location
    for row in sales_rows:
        row["location"] = primary_location
    sites = _build_sites(rng, policy.site_count, list(stock_by_sku.keys()))

    product_by_sku = {row["sku"]: row for row in product_rows}
    sales_by_sku: dict[str, list[dict[str, Any]]] = {}
    for row in sales_rows:
        sales_by_sku.setdefault(row["sku"], []).append(row)
    near_expiry = _prefer_profitable_markdown(
        near_expiry, product_by_sku=product_by_sku, sales_by_sku=sales_by_sku
    )

    hero_candidates = near_expiry or low_stock or [product_rows[0]["sku"]]
    sales_by_sku: dict[str, list[int]] = {}
    for row in sales_rows:
        sales_by_sku.setdefault(str(row["sku"]), []).append(int(row["quantity"]))
    hero_sku = max(
        hero_candidates,
        key=lambda sku: (
            sum(sales_by_sku.get(sku, ())) / max(len(sales_by_sku.get(sku, ())), 1)
        )
        / max(int(stock_by_sku[sku]["on_hand"]), 1),
    )

    payload: dict[str, Any] = {
        "products": product_rows,
        "stock": list(stock_by_sku.values()),
        "sales": sales_rows,
        "suppliers": list(suppliers.values()),
        "sites": sites,
        "price_anomalies": price_anomalies,
        "constraints": {
            "near_expiry_skus": near_expiry,
            "low_stock_skus": low_stock,
            "delayed_supplier_ids": delayed_suppliers,
            "price_anomaly_skus": [row["sku"] for row in price_anomalies],
            "hero_sku": hero_sku,
        },
    }

    store.save(
        {
            "tenant_id": tenant_id,
            "seed": policy.seed,
            "policy": policy.name,
            "generated_at": now.isoformat(),
            "payload": payload,
        }
    )

    return PopulationReceipt(
        tenant_id=tenant_id,
        seed=policy.seed,
        policy=policy.name,
        product_count=len(product_rows),
        supplier_count=len(suppliers),
        site_count=len(sites),
        near_expiry_skus=tuple(near_expiry),
        low_stock_skus=tuple(low_stock),
        delayed_supplier_ids=tuple(delayed_suppliers),
        price_anomaly_skus=tuple(row["sku"] for row in price_anomalies),
        hero_sku=hero_sku,
    )


def _product_row(product: Any) -> dict[str, Any]:
    price = round(product.price_cents / 100, 2)
    cost = round(price * 0.65, 2)
    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "department": product.department,
        "subcategory": product.subcategory,
        "supplier": product.supplier,
        "unit_cost": cost,
        "unit_price": price,
        "shelf_location": product.shelf_location,
        "physics": product.physics,
    }


def _stock_row(rng: Random, product: Any, now: datetime) -> dict[str, Any]:
    """Create a current SKU row with independently traceable active lots."""
    # Preserve the original global random sequence. The later policy-selection pass
    # depends on it for stable demo scenarios.
    on_hand = rng.randint(15, 80)
    reorder_point = rng.randint(5, 25)
    expiry_days = rng.randint(10, 90)
    received_days_ago = rng.randint(0, 6)
    perishable_physics = {
        "dairy", "meat", "poultry", "seafood", "deli", "chilled_other", "frozen"
    }
    lot_count = (
        2 + zlib.crc32(f"{product.sku}:lot-count".encode()) % 2
        if product.physics in perishable_physics
        else 1
    )
    batches = _batch_rows(
        product,
        now,
        total_units=on_hand,
        expiry_days=expiry_days,
        received_days_ago=received_days_ago,
        lot_count=lot_count,
    )
    return {
        "sku": product.sku,
        "location": "generated",
        "on_hand": on_hand,
        "reorder_point": reorder_point,
        # These aggregate fields preserve the existing scenario contract. Batch-level
        # consumers use ``batches`` below and must not infer one lot from this row.
        "expiry_date": (now + timedelta(days=expiry_days)).date().isoformat(),
        "received_date": (now - timedelta(days=received_days_ago)).date().isoformat(),
        "batches": batches,
    }


def _batch_rows(
    product: Any,
    now: datetime,
    *,
    total_units: int,
    expiry_days: int,
    received_days_ago: int,
    lot_count: int,
) -> list[dict[str, Any]]:
    """Create deterministic batch facts that reconcile to the SKU-level stock total."""
    local_rng = Random(zlib.crc32(f"{product.sku}:lots".encode()))
    remaining = total_units
    rows: list[dict[str, Any]] = []
    for index in range(lot_count):
        units = (
            remaining
            if index == lot_count - 1
            else local_rng.randint(1, remaining - (lot_count - index - 1))
        )
        remaining -= units
        batch_expiry_days = (
            expiry_days if index == 0 else expiry_days + local_rng.randint(1, 45)
        )
        batch_received_days_ago = (
            received_days_ago if index == 0 else local_rng.randint(0, received_days_ago)
        )
        rows.append(
            {
                "lot_id": f"LOT-{product.sku}-{index + 1:02d}",
                "on_hand": units,
                "reserved": 0,
                "damaged": 0,
                "expiry_date": (now + timedelta(days=batch_expiry_days)).date().isoformat(),
                "received_date": (now - timedelta(days=batch_received_days_ago)).date().isoformat(),
                "source_system": "worldgen",
                "source_confidence": "high",
            }
        )
    return rows


def _sales_rows(rng: Random, product: Any, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for day_offset in range(1, 15):
        quantity = rng.randint(1, 15)
        ts = (now - timedelta(days=day_offset)).replace(hour=10, minute=0, second=0)
        rows.append(
            {
                "sku": product.sku,
                "location": "generated",
                "ts": ts.isoformat(),
                "quantity": quantity,
                "unit_price": round(product.price_cents / 100, 2),
            }
        )
    return rows


def _build_supplier(rng: Random, name: str) -> dict[str, Any]:
    return {
        "supplier_id": f"supplier:{name.lower().replace(' ', '_')}",
        "name": name,
        "lead_time_days": round(rng.uniform(1.0, 5.0), 2),
        "fill_rate": round(rng.uniform(0.70, 0.98), 2),
        "recent_delay": False,
        "distance_km": round(rng.uniform(80.0, 250.0), 1),
        "available_units": rng.randint(500, 2000),
    }


def _select_near_expiry(
    rng: Random, stock_by_sku: dict[str, dict[str, Any]], count: int, now: datetime
) -> list[str]:
    if count <= 0:
        return []
    skus = list(stock_by_sku.keys())
    rng.shuffle(skus)
    chosen = skus[:count]
    for sku in chosen:
        days = rng.randint(2, 3)
        received_date = (now - timedelta(days=rng.randint(1, 4))).date().isoformat()
        stock = stock_by_sku[sku]
        batches = stock.get("batches") or []
        if batches:
            earliest = batches[0]
            earliest["expiry_date"] = (now + timedelta(days=days)).date().isoformat()
            earliest["received_date"] = received_date
        stock["expiry_date"] = (now + timedelta(days=days)).date().isoformat()
        stock["received_date"] = received_date
    return chosen


def _prefer_profitable_markdown(
    near_expiry: list[str],
    *,
    product_by_sku: dict[str, dict[str, Any]],
    sales_by_sku: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Reorder so a genuinely markdown-profitable SKU (per real simulate_markdown math)
    leads the list and becomes hero_sku - never hardcode which one, compute it."""
    if len(near_expiry) <= 1:
        return near_expiry

    def is_profitable(sku: str) -> bool:
        product = product_by_sku[sku]
        sales = sales_by_sku.get(sku) or []
        if not sales:
            return False
        recent = [Decimal(str(row["quantity"])) for row in sales[-14:]]
        base_daily = sum(recent) / Decimal(len(recent))
        result = simulate_markdown(
            sku=sku,
            units_on_hand=Decimal("30"),
            days_to_expiry=Decimal("2"),
            base_daily_units=base_daily,
            unit_price=Money.zar(str(product["unit_price"])),
            unit_cost=Money.zar(str(product["unit_cost"])),
            discount_pct=Decimal("0.2"),
        )
        return result.incremental_profit.minor_units > 0

    profitable = [sku for sku in near_expiry if is_profitable(sku)]
    rest = [sku for sku in near_expiry if sku not in profitable]
    return profitable + rest if profitable else near_expiry


def _select_low_stock(
    rng: Random, stock_by_sku: dict[str, dict[str, Any]], *, exclude: set[str], count: int
) -> list[str]:
    if count <= 0:
        return []
    candidates = [sku for sku in stock_by_sku if sku not in exclude]
    rng.shuffle(candidates)
    chosen = candidates[:count]
    for sku in chosen:
        reorder_point = stock_by_sku[sku]["reorder_point"]
        stock_by_sku[sku]["on_hand"] = max(0, reorder_point - rng.randint(1, 5))
    return chosen


def _select_delayed_suppliers(
    rng: Random, suppliers: dict[str, dict[str, Any]], count: int
) -> list[str]:
    if count <= 0:
        return []
    names = list(suppliers.keys())
    rng.shuffle(names)
    chosen_names = names[:count]
    chosen_ids = []
    for name in chosen_names:
        suppliers[name]["recent_delay"] = True
        suppliers[name]["lead_time_days"] = round(
            suppliers[name]["lead_time_days"] + rng.uniform(2.0, 5.0), 2
        )
        chosen_ids.append(suppliers[name]["supplier_id"])
    return chosen_ids


def _select_price_anomalies(
    rng: Random, product_rows: list[dict[str, Any]], *, exclude: set[str], count: int
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    candidates = [row for row in product_rows if row["sku"] not in exclude]
    rng.shuffle(candidates)
    anomalies = []
    for row in candidates[:count]:
        direction = rng.choice([1, -1])
        delta_pct = rng.uniform(0.18, 0.35) * direction
        observed = round(row["unit_price"] * (1 + delta_pct), 2)
        anomalies.append({"sku": row["sku"], "observed_unit_price": observed})
    return anomalies


def _build_sites(rng: Random, site_count: int, skus: list[str]) -> list[dict[str, Any]]:
    site_count = max(1, site_count - 1)
    branch_names = [
        f"store_{rng.randint(1, 99):02d}_{suffix}"
        for suffix in rng.sample(
            ["sandton", "midrand", "rosebank", "fourways", "soweto", "centurion"],
            k=min(site_count, 6),
        )
    ]
    sites: list[dict[str, Any]] = []
    sample_skus = rng.sample(skus, k=min(len(skus), 40))
    for name in branch_names:
        sites.append(
            {
                "site_type": "branch",
                "site_id": name,
                "distance_km": round(rng.uniform(3.0, 45.0), 1),
                "lead_time_hours": round(rng.uniform(1.5, 8.0), 1),
                "stock": {sku: rng.randint(0, 25) for sku in sample_skus},
            }
        )
    sites.append(
        {
            "site_type": "distribution_center",
            "site_id": f"dc_{rng.randint(1, 99):02d}",
            "distance_km": round(rng.uniform(50.0, 120.0), 1),
            "lead_time_hours": round(rng.uniform(12.0, 30.0), 1),
            "stock": dict.fromkeys(sample_skus, rng.randint(200, 800)),
        }
    )
    return sites
