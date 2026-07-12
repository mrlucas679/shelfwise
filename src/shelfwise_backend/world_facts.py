"""Read-only facts drawn from the generated Postgres-backed world.

Replaces `shelfwise_data.load_seeded_scenario` / `build_store_intelligence_demo`: instead of
CSV rows and hand-typed literals, every fact returned here comes from a deterministic,
policy-constrained world persisted per tenant in `shelfwise_world_snapshot` (memory or
Postgres, depending on `SHELFWISE_STORE_BACKEND`). If a tenant has no snapshot yet, one is
generated on first access from `DEMO_POLICY` so existing zero-config flows keep working.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from shelfwise_contracts import Money
from shelfwise_data.seed import SeededScenario
from shelfwise_data.store_intelligence import (
    DecisionOutcome,
    DeliveryReceipt,
    StockBatch,
    SupplierCoverRequest,
    plan_supplier_cover,
    reconcile_delivery,
    split_stock_by_fefo,
    summarize_outcome,
)
from shelfwise_decision_science import StockSourceCandidate, plan_stock_sourcing
from shelfwise_worldgen.populate import DEMO_POLICY, WorldSnapshotStoreLike, populate_world

REFERENCE_NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


class UnknownSkuError(ValueError):
    """Raised when a tenant's generated world has no product matching the requested SKU."""


class WorldFactsProvider:
    """Query the generated world for one tenant - the single source of retail facts."""

    def __init__(self, store: WorldSnapshotStoreLike, *, now: datetime = REFERENCE_NOW) -> None:
        self._store = store
        self._now = now

    def get_hero_sku(self, tenant_id: str) -> str:
        return str(self._snapshot(tenant_id)["constraints"]["hero_sku"])

    def search_products(
        self, tenant_id: str, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        needle = query.strip().lower()
        products = self._snapshot(tenant_id)["products"]
        if not needle:
            return list(products[:limit])
        matches = [
            row
            for row in products
            if needle in row["name"].lower()
            or needle in row["category"].lower()
            or needle in row["sku"].lower()
        ]
        return matches[:limit]

    def get_scenario_facts(self, tenant_id: str, sku: str | None = None) -> SeededScenario:
        payload = self._snapshot(tenant_id)
        resolved_sku = sku or str(payload["constraints"]["hero_sku"])
        product = self._product_row(payload, resolved_sku)
        stock = self._stock_row(payload, resolved_sku)
        supplier = self._supplier_row(payload, product["supplier"])
        recent_daily_units = self._recent_daily_units(payload, resolved_sku)
        expiry = date.fromisoformat(stock["expiry_date"])
        days_to_expiry = max(0, (expiry - self._now.date()).days)
        return SeededScenario(
            sku=resolved_sku,
            product_name=product["name"],
            category=product["category"],
            supplier=product["supplier"],
            location=stock["location"],
            units_on_hand=stock["on_hand"],
            reorder_point=stock["reorder_point"],
            days_to_expiry=days_to_expiry,
            recent_daily_units=recent_daily_units,
            unit_cost=Money.zar(product["unit_cost"]),
            unit_price=Money.zar(product["unit_price"]),
            supplier_lead_time_days=Decimal(str(supplier["lead_time_days"])),
            supplier_recent_delay=bool(supplier["recent_delay"]),
            datasets_dir=Path("."),
        )

    def get_sourcing_candidates(
        self, tenant_id: str, sku: str, *, units_needed: int | None = None
    ) -> tuple[StockSourceCandidate, ...]:
        payload = self._snapshot(tenant_id)
        product = self._product_row(payload, sku)
        unit_cost = Decimal(str(product["unit_cost"]))
        candidates = [
            StockSourceCandidate(
                source_type=site["site_type"],
                source_id=site["site_id"],
                available_units=int(site["stock"].get(sku, 0)),
                distance_km=Decimal(str(site["distance_km"])),
                lead_time_hours=Decimal(str(site["lead_time_hours"])),
                unit_cost=unit_cost,
            )
            for site in payload["sites"]
        ]
        supplier = self._supplier_row(payload, product["supplier"])
        candidates.append(
            StockSourceCandidate(
                source_type="supplier",
                source_id=supplier["supplier_id"],
                available_units=int(supplier["available_units"]),
                distance_km=Decimal(str(supplier["distance_km"])),
                lead_time_hours=Decimal(str(supplier["lead_time_days"])) * Decimal("24"),
                unit_cost=unit_cost,
            )
        )
        return tuple(candidates)

    def get_store_intelligence(self, tenant_id: str) -> dict[str, Any]:
        payload = self._snapshot(tenant_id)
        hero_sku = str(payload["constraints"]["hero_sku"])
        hero_stock = self._stock_row(payload, hero_sku)

        batch = StockBatch(
            sku=hero_sku,
            lot=f"LOT-{hero_sku}",
            units=hero_stock["on_hand"],
            expiry_date=date.fromisoformat(hero_stock["expiry_date"]),
            received_date=date.fromisoformat(hero_stock["received_date"]),
            location=hero_stock["location"],
        )
        batch_split = split_stock_by_fefo(sku=hero_sku, as_of=self._now.date(), batches=(batch,))

        low_stock_skus = payload["constraints"].get("low_stock_skus") or []
        delivery_sku = str(low_stock_skus[0]) if low_stock_skus else hero_sku
        delivery_stock = self._stock_row(payload, delivery_sku)
        ordered = max(delivery_stock["reorder_point"] * 3, 1)
        received = max(0, ordered - delivery_stock["reorder_point"])
        delivery = reconcile_delivery(
            DeliveryReceipt(
                sku=delivery_sku,
                ordered_units=ordered,
                asn_units=ordered,
                received_units=received,
                accepted_units=received,
                short_dated_units=0,
            )
        )

        delivery_product = self._product_row(payload, delivery_sku)
        delivery_supplier = self._supplier_row(payload, delivery_product["supplier"])
        forecast_daily = self._forecast_daily(payload, delivery_sku)
        max_site_stock = max(
            (int(site["stock"].get(delivery_sku, 0)) for site in payload["sites"]), default=0
        )
        supplier_cover = plan_supplier_cover(
            SupplierCoverRequest(
                sku=delivery_sku,
                units_on_hand=delivery_stock["on_hand"],
                forecast_daily_units=forecast_daily,
                supplier_lead_time_days=Decimal(str(delivery_supplier["lead_time_days"])),
                transfer_available_units=max_site_stock,
            )
        )

        stock_sourcing = None
        if supplier_cover.gap_before_delivery_units > 0:
            candidates = self.get_sourcing_candidates(tenant_id, delivery_sku)
            stock_sourcing = plan_stock_sourcing(
                sku=delivery_sku,
                units_needed=supplier_cover.gap_before_delivery_units,
                candidates=candidates,
            ).to_dict()

        recent_sales = self._recent_daily_units(payload, hero_sku)
        predicted = max(1, round(float(forecast_daily) * 7))
        actual = int(sum(recent_sales[-7:])) if recent_sales else predicted
        waste_baseline = max(1, hero_stock["reorder_point"] // 4)
        learning = summarize_outcome(
            DecisionOutcome(
                sku=hero_sku,
                action="markdown",
                predicted_sell_through_units=predicted,
                actual_sell_through_units=max(actual, 1),
                predicted_waste_units=waste_baseline,
                actual_waste_units=max(1, waste_baseline - 1),
            )
        )

        return {
            "batch_split": batch_split.to_dict(),
            "delivery_reconciliation": delivery.to_dict(),
            "supplier_cover": supplier_cover.to_dict(),
            "stock_sourcing": stock_sourcing,
            "learning_summary": learning.to_dict(),
        }

    def _snapshot(self, tenant_id: str) -> dict[str, Any]:
        snapshot = self._store.get(tenant_id)
        if snapshot is None:
            populate_world(DEMO_POLICY, tenant_id=tenant_id, store=self._store, now=self._now)
            snapshot = self._store.get(tenant_id)
        assert snapshot is not None  # populate_world always saves before returning
        return snapshot["payload"]

    def _product_row(self, payload: dict[str, Any], sku: str) -> dict[str, Any]:
        for row in payload["products"]:
            if row["sku"] == sku:
                return row
        raise UnknownSkuError(f"unknown sku: {sku!r}")

    def _stock_row(self, payload: dict[str, Any], sku: str) -> dict[str, Any]:
        for row in payload["stock"]:
            if row["sku"] == sku:
                return row
        raise UnknownSkuError(f"no stock position for sku: {sku!r}")

    def _supplier_row(self, payload: dict[str, Any], supplier_name: str) -> dict[str, Any]:
        for row in payload["suppliers"]:
            if row["name"] == supplier_name:
                return row
        raise UnknownSkuError(f"unknown supplier: {supplier_name!r}")

    def _recent_daily_units(self, payload: dict[str, Any], sku: str) -> tuple[Decimal, ...]:
        sales = [row for row in payload["sales"] if row["sku"] == sku]
        sales.sort(key=lambda row: row["ts"])
        return tuple(Decimal(row["quantity"]) for row in sales[-14:])

    def _forecast_daily(self, payload: dict[str, Any], sku: str) -> Decimal:
        recent = self._recent_daily_units(payload, sku)
        if not recent:
            return Decimal("1")
        return (sum(recent) / len(recent)).quantize(Decimal("0.01"))
