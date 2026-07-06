from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from shelfwise_contracts import Money

REFERENCE_NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
HERO_SKU = "4011"
DEFAULT_DATASETS = Path(__file__).resolve().parents[2] / "data" / "datasets"


@dataclass(frozen=True, slots=True)
class ProductRow:
    sku: str
    name: str
    category: str
    supplier: str
    shelf_life_days: int
    cost: Money
    price: Money


@dataclass(frozen=True, slots=True)
class StockRow:
    sku: str
    location: str
    on_hand: int
    reorder_point: int
    expiry_date: date


@dataclass(frozen=True, slots=True)
class SaleRow:
    ts: datetime
    sku: str
    location: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class SupplierRow:
    supplier: str
    avg_lead_time_days: Decimal
    recent_delay: bool


@dataclass(frozen=True, slots=True)
class SeededScenario:
    sku: str
    product_name: str
    category: str
    supplier: str
    location: str
    units_on_hand: int
    reorder_point: int
    days_to_expiry: int
    recent_daily_units: tuple[Decimal, ...]
    unit_cost: Money
    unit_price: Money
    supplier_lead_time_days: Decimal
    supplier_recent_delay: bool
    datasets_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "product_name": self.product_name,
            "category": self.category,
            "supplier": self.supplier,
            "location": self.location,
            "units_on_hand": self.units_on_hand,
            "reorder_point": self.reorder_point,
            "days_to_expiry": self.days_to_expiry,
            "recent_daily_units": [str(unit) for unit in self.recent_daily_units],
            "unit_cost": self.unit_cost.to_dict(),
            "unit_price": self.unit_price.to_dict(),
            "supplier_lead_time_days": str(self.supplier_lead_time_days),
            "supplier_recent_delay": self.supplier_recent_delay,
            "datasets_dir": str(self.datasets_dir),
        }


def load_products(path: Path = DEFAULT_DATASETS / "products.csv") -> list[ProductRow]:
    rows = _read_csv(
        path,
        {"sku", "name", "category", "supplier", "shelf_life_days", "cost", "price"},
    )
    return [
        ProductRow(
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            supplier=row["supplier"],
            shelf_life_days=_non_negative_int(row["shelf_life_days"], "shelf_life_days"),
            cost=Money.zar(row["cost"]),
            price=Money.zar(row["price"]),
        )
        for row in rows
    ]


def load_stock(path: Path = DEFAULT_DATASETS / "stock.csv") -> list[StockRow]:
    rows = _read_csv(path, {"sku", "location", "on_hand", "reorder_point", "expiry_date"})
    return [
        StockRow(
            sku=row["sku"],
            location=row["location"],
            on_hand=_non_negative_int(row["on_hand"], "on_hand"),
            reorder_point=_non_negative_int(row["reorder_point"], "reorder_point"),
            expiry_date=date.fromisoformat(row["expiry_date"]),
        )
        for row in rows
    ]


def load_sales(path: Path = DEFAULT_DATASETS / "sales.csv") -> list[SaleRow]:
    rows = _read_csv(path, {"ts", "sku", "location", "quantity", "unit_price"})
    return [
        SaleRow(
            ts=_datetime(row["ts"]),
            sku=row["sku"],
            location=row["location"],
            quantity=_positive_int(row["quantity"], "quantity"),
            unit_price=Decimal(row["unit_price"]),
        )
        for row in rows
    ]


def load_suppliers(path: Path = DEFAULT_DATASETS / "suppliers.csv") -> list[SupplierRow]:
    rows = _read_csv(path, {"supplier", "avg_lead_time_days", "recent_delay"})
    return [
        SupplierRow(
            supplier=row["supplier"],
            avg_lead_time_days=Decimal(row["avg_lead_time_days"]),
            recent_delay=row["recent_delay"].strip().lower() == "true",
        )
        for row in rows
    ]


def load_seeded_scenario(
    datasets_dir: Path = DEFAULT_DATASETS,
    *,
    now: datetime = REFERENCE_NOW,
    sku: str = HERO_SKU,
) -> SeededScenario:
    datasets_dir = Path(datasets_dir)
    validate_seed_data(datasets_dir, now=now, hero_sku=sku)

    products = {item.sku: item for item in load_products(datasets_dir / "products.csv")}
    stock_rows = [item for item in load_stock(datasets_dir / "stock.csv") if item.sku == sku]
    if not stock_rows:
        raise ValueError(f"seed stock missing hero SKU: {sku}")
    stock = stock_rows[0]
    product = products[sku]
    suppliers = {
        item.supplier: item for item in load_suppliers(datasets_dir / "suppliers.csv")
    }
    supplier = suppliers.get(product.supplier)
    if supplier is None:
        raise ValueError(f"products.csv references unknown supplier: {product.supplier}")

    return SeededScenario(
        sku=sku,
        product_name=product.name,
        category=product.category,
        supplier=product.supplier,
        location=stock.location,
        units_on_hand=stock.on_hand,
        reorder_point=stock.reorder_point,
        days_to_expiry=max(0, (stock.expiry_date - now.date()).days),
        recent_daily_units=recent_daily_units(
            load_sales(datasets_dir / "sales.csv"),
            sku=sku,
            location=stock.location,
        ),
        unit_cost=product.cost,
        unit_price=product.price,
        supplier_lead_time_days=supplier.avg_lead_time_days,
        supplier_recent_delay=supplier.recent_delay,
        datasets_dir=datasets_dir,
    )


def validate_seed_data(
    datasets_dir: Path = DEFAULT_DATASETS,
    *,
    now: datetime = REFERENCE_NOW,
    hero_sku: str = HERO_SKU,
) -> None:
    products = {item.sku: item for item in load_products(datasets_dir / "products.csv")}
    stock = load_stock(datasets_dir / "stock.csv")
    sales = load_sales(datasets_dir / "sales.csv")
    suppliers = {item.supplier for item in load_suppliers(datasets_dir / "suppliers.csv")}

    _reject_unknown_skus("stock.csv", (item.sku for item in stock), products.keys())
    _reject_unknown_skus("sales.csv", (item.sku for item in sales), products.keys())

    missing_suppliers = sorted(
        {item.supplier for item in products.values()} - suppliers
    )
    if missing_suppliers:
        raise ValueError(f"products.csv references unknown suppliers: {missing_suppliers}")

    hero_stock = [item for item in stock if item.sku == hero_sku]
    if not hero_stock:
        raise ValueError(f"planted story hero SKU missing from stock.csv: {hero_sku}")
    hero = hero_stock[0]
    days_to_expiry = (hero.expiry_date - now.date()).days
    if hero.on_hand <= hero.reorder_point or not 0 <= days_to_expiry <= 5:
        raise ValueError(
            "planted story broken: "
            f"sku={hero_sku}, on_hand={hero.on_hand}, "
            f"reorder_point={hero.reorder_point}, days_to_expiry={days_to_expiry}"
        )
    if not recent_daily_units(sales, sku=hero_sku, location=hero.location):
        raise ValueError(f"planted story hero SKU missing sales history: {hero_sku}")


def recent_daily_units(
    sales: list[SaleRow],
    *,
    sku: str,
    location: str | None = None,
    limit: int = 14,
) -> tuple[Decimal, ...]:
    if limit <= 0:
        raise ValueError("limit must be positive")

    totals: dict[date, int] = defaultdict(int)
    for row in sales:
        if row.sku != sku:
            continue
        if location is not None and row.location != location:
            continue
        totals[row.ts.date()] += row.quantity

    ordered = [Decimal(totals[item]) for item in sorted(totals)]
    return tuple(ordered[-limit:])


def _read_csv(path: Path, required_columns: set[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"dataset missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = sorted(required_columns - headers)
        if missing:
            raise ValueError(f"{path.name} missing columns: {missing}")
        return list(reader)


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _non_negative_int(value: str, field_name: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


def _positive_int(value: str, field_name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive")
    return parsed


def _reject_unknown_skus(
    filename: str,
    row_skus: Any,
    product_skus: Any,
) -> None:
    unknown = sorted(set(row_skus) - set(product_skus))
    if unknown:
        raise ValueError(f"{filename} references unknown SKUs: {unknown}")
