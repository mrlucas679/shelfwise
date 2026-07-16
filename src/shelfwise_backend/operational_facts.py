"""Read measured retail facts from the operational stores without demo fallbacks."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from shelfwise_contracts import Event, EventSource, EventType, Money
from shelfwise_runtime import DataDomain
from shelfwise_twin import FreshnessState, StateLane, TwinService

from .retail_intelligence import SeededScenario


class MissingOperationalFacts(ValueError):
    """Report facts that must be measured before an operational decision can run."""

    def __init__(self, missing: list[str] | tuple[str, ...]) -> None:
        self.missing = tuple(sorted(dict.fromkeys(str(item) for item in missing if item)))
        super().__init__("missing operational facts: " + ", ".join(self.missing))


class OperationalFactsProvider:
    """Event-scoped view over reported twin, catalog, and inventory state."""

    source_dataset = DataDomain.OPERATIONAL_TWIN.value
    source_method = "reported_operational_twin"
    data_domain = DataDomain.OPERATIONAL_TWIN.value

    def __init__(
        self,
        event: Event,
        *,
        twin_service: TwinService,
        product_catalog_store: Any,
        inventory_position_store: Any,
    ) -> None:
        if event.data_domain is not DataDomain.OPERATIONAL_TWIN:
            raise ValueError("OperationalFactsProvider requires an operational event")
        self._event = event
        self._twin = twin_service
        self._catalog = product_catalog_store
        self._inventory = inventory_position_store
        self._store_id = _first_text(
            event.payload,
            "store_id",
            "site_id",
            "location",
            "location_id",
        )

    @classmethod
    def for_query(
        cls,
        tenant_id: str,
        *,
        twin_service: TwinService,
        product_catalog_store: Any,
        inventory_position_store: Any,
        store_id: str | None = None,
    ) -> OperationalFactsProvider:
        """Create a read-only provider for API queries that are not caused by an event."""
        return cls(
            Event(
                id=f"query_{tenant_id}_{store_id or 'all'}",
                type=EventType.SCAN,
                ts=datetime.now(UTC),
                actor="backend_query",
                payload={"store_id": store_id} if store_id else {},
                source=EventSource.API,
                tenant_id=tenant_id,
                data_domain=DataDomain.OPERATIONAL_TWIN,
            ),
            twin_service=twin_service,
            product_catalog_store=product_catalog_store,
            inventory_position_store=inventory_position_store,
        )

    def get_hero_sku(self, tenant_id: str) -> str:
        self._require_tenant(tenant_id)
        sku = _first_text(self._event.payload, "sku")
        if sku:
            return sku
        products = self.list_products(tenant_id)
        skus = [str(item.get("sku") or "") for item in products if item.get("sku")]
        if len(skus) == 1:
            return skus[0]
        raise MissingOperationalFacts(["sku"])

    def get_scenario_facts(self, tenant_id: str, sku: str | None = None) -> SeededScenario:
        """Build the compatibility scenario only when every value is measured."""
        self._require_tenant(tenant_id)
        resolved_sku = sku or self.get_hero_sku(tenant_id)
        payload = self._event.payload
        product = self._product_for_sku(tenant_id, resolved_sku)
        product_props = self._properties_for("product", resolved_sku)

        product_name = _first_text(payload, "product", "product_name") or _text(
            product.get("name")
        ) or self._entity_display_name("product", resolved_sku)
        category = (
            _first_text(payload, "category")
            or _text(product.get("category"))
            or _text(product_props.get("catalog.category"))
        )
        location = self._store_id
        on_hand = _first_number(payload, "on_hand", "units_on_hand")
        if on_hand is None:
            on_hand = self._inventory_quantity(tenant_id, resolved_sku)
        if on_hand is None:
            on_hand = _number(product_props.get("inventory.on_hand"))
        reorder_point = _first_number(payload, "reorder_point")
        if reorder_point is None:
            reorder_point = _number(product_props.get("inventory.reorder_point"))
        days_to_expiry = _first_number(payload, "days_to_expiry")
        if days_to_expiry is None:
            days_to_expiry = _number(product_props.get("expiry.days_to_expiry"))

        recent = self._payload_recent_units(payload)
        if not recent:
            recent = self.get_recent_daily_units(tenant_id, resolved_sku)

        unit_cost = _money_from_fields(
            payload,
            amount_fields=("unit_cost", "catalog_unit_cost"),
            minor_fields=("unit_cost_cents", "unit_cost_minor_units"),
        ) or _money_from_property(product_props.get("catalog.unit_cost_minor_units"))
        unit_price = _money_from_fields(
            payload,
            amount_fields=("catalog_unit_price",),
            minor_fields=("catalog_price_cents", "catalog_unit_price_minor_units"),
        ) or _money_from_property(
            product_props.get(
                "catalog.unit_price_minor_units",
                product_props.get("sales.catalog_price_cents"),
            )
        )

        supplier = _first_text(payload, "supplier", "supplier_id") or _text(
            product_props.get("sourcing.supplier_id")
        )
        supplier_props = self._properties_for("supplier", supplier) if supplier else {}
        lead_time = _first_decimal(payload, "lead_time_days", "avg_lead_time_days")
        if lead_time is None:
            lead_time = _decimal(supplier_props.get("supplier.lead_time_days"))
        recent_delay = _first_bool(payload, "recent_delay")
        if recent_delay is None:
            recent_delay = _bool(supplier_props.get("supplier.recent_delay"))

        values = {
            "product_name": product_name,
            "category": category,
            "location": location,
            "units_on_hand": on_hand,
            "reorder_point": reorder_point,
            "days_to_expiry": days_to_expiry,
            "recent_daily_units": recent,
            "unit_cost": unit_cost,
            "unit_price": unit_price,
            "supplier": supplier,
            "supplier_lead_time_days": lead_time,
            "supplier_recent_delay": recent_delay,
        }
        missing = [name for name, value in values.items() if value is None or value == ()]
        if missing:
            raise MissingOperationalFacts(missing)

        return SeededScenario(
            sku=resolved_sku,
            product_name=str(product_name),
            category=str(category),
            supplier=str(supplier),
            location=str(location),
            units_on_hand=int(on_hand),
            reorder_point=int(reorder_point),
            days_to_expiry=int(days_to_expiry),
            recent_daily_units=tuple(recent),
            unit_cost=unit_cost,
            unit_price=unit_price,
            supplier_lead_time_days=Decimal(lead_time),
            supplier_recent_delay=bool(recent_delay),
            datasets_dir=Path("."),
        )

    def get_recent_daily_units(
        self, tenant_id: str, sku: str, *, limit: int = 14
    ) -> tuple[Decimal, ...]:
        self._require_tenant(tenant_id)
        if limit <= 0 or limit > 90:
            raise ValueError("limit must be between 1 and 90")
        observations = self._twin.store.list_observations(
            tenant_id,
            store_id=self._store_id,
            limit=500,
        )
        values = [
            _decimal(item.value)
            for item in reversed(observations)
            if item.lane is StateLane.REPORTED
            and item.property_name == "sales.units"
            and _local_id(item.twin_id) == _slug(sku)
            and _decimal(item.value) is not None
        ]
        return tuple(value for value in values[-limit:] if value is not None)

    def get_supplier_for_sku(self, tenant_id: str, sku: str) -> dict[str, Any]:
        scenario = self.get_scenario_facts(tenant_id, sku)
        props = self._properties_for("supplier", scenario.supplier)
        fill_rate = _first_decimal(self._event.payload, "fill_rate")
        if fill_rate is None:
            fill_rate = _decimal(props.get("supplier.fill_rate"))
        if fill_rate is None:
            raise MissingOperationalFacts(["supplier_fill_rate"])
        return {
            "supplier_id": scenario.supplier,
            "name": scenario.supplier,
            "lead_time_days": scenario.supplier_lead_time_days,
            "fill_rate": fill_rate,
            "recent_delay": scenario.supplier_recent_delay,
        }

    def get_alternate_supplier(self, tenant_id: str, *, exclude: str) -> dict[str, Any] | None:
        self._require_tenant(tenant_id)
        candidates: list[dict[str, Any]] = []
        for entity in self._twin.store.list_entities(tenant_id, store_id=self._store_id):
            if entity.entity_type != "supplier" or _local_id(entity.twin_id) == _slug(exclude):
                continue
            props = self._properties_for("supplier", _local_id(entity.twin_id))
            lead = _decimal(props.get("supplier.lead_time_days"))
            fill = _decimal(props.get("supplier.fill_rate"))
            if lead is None or fill is None:
                continue
            candidates.append(
                {
                    "supplier_id": _local_id(entity.twin_id),
                    "name": entity.display_name,
                    "lead_time_days": lead,
                    "fill_rate": fill,
                }
            )
        return min(candidates, key=lambda item: item["lead_time_days"]) if candidates else None

    def list_products(self, tenant_id: str) -> list[dict[str, Any]]:
        self._require_tenant(tenant_id)
        rows: list[dict[str, Any]] = []
        for product in self._catalog.list_products(tenant_id=tenant_id):
            variants = self._catalog.list_variants(
                tenant_id=tenant_id,
                product_id=str(product["product_id"]),
            )
            for variant in variants:
                identifiers = self._catalog.list_identifiers(
                    tenant_id=tenant_id,
                    variant_id=str(variant["variant_id"]),
                )
                sku = next(
                    (str(item["value"]) for item in identifiers if item.get("kind") == "sku"),
                    None,
                )
                if sku:
                    props = self._properties_for("product", sku)
                    supplier = _text(props.get("sourcing.supplier_id"))
                    unit_cost = _money_from_property(
                        props.get("catalog.unit_cost_minor_units")
                    )
                    unit_price = _money_from_property(
                        props.get(
                            "catalog.unit_price_minor_units",
                            props.get("sales.catalog_price_cents"),
                        )
                    )
                    rows.append(
                        {
                            **product,
                            **variant,
                            "sku": sku,
                            "supplier": supplier,
                            "unit_cost": unit_cost.amount if unit_cost else None,
                            "unit_price": unit_price.amount if unit_price else None,
                            "physics": _text(product.get("category")),
                        }
                    )
        return rows

    def search_products(
        self, tenant_id: str, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        needle = query.strip().lower()
        rows = self.list_products(tenant_id)
        if needle:
            rows = [
                row
                for row in rows
                if any(
                    needle in str(row.get(field) or "").lower()
                    for field in ("name", "category", "brand", "sku")
                )
            ]
        return rows[:limit]

    def list_stock(self, tenant_id: str) -> list[dict[str, Any]]:
        self._require_tenant(tenant_id)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for position in self._inventory.list(tenant_id=tenant_id):
            key = (str(position["sku"]), str(position["location_id"]))
            row = rows.setdefault(
                key,
                {
                    "sku": key[0],
                    "location": key[1],
                    "on_hand": 0,
                    "source": DataDomain.OPERATIONAL_TWIN.value,
                },
            )
            row["on_hand"] += int(position["quantity"])
        for entity in self._twin.store.list_entities(tenant_id, store_id=self._store_id):
            if entity.entity_type != "product":
                continue
            sku = _local_id(entity.twin_id)
            props = self._properties_for("product", sku)
            on_hand = _number(props.get("inventory.on_hand"))
            reorder_point = _number(props.get("inventory.reorder_point"))
            days_to_expiry = _number(props.get("expiry.days_to_expiry"))
            if on_hand is None and reorder_point is None and days_to_expiry is None:
                continue
            key = (sku, entity.store_id)
            row = rows.setdefault(
                key,
                {
                    "sku": sku,
                    "location": entity.store_id,
                    "on_hand": on_hand or 0,
                    "source": DataDomain.OPERATIONAL_TWIN.value,
                },
            )
            if on_hand is not None and not self._inventory.list(tenant_id=tenant_id, sku=sku):
                row["on_hand"] = on_hand
            if reorder_point is not None:
                row["reorder_point"] = reorder_point
            if days_to_expiry is not None:
                expiry = (datetime.now(UTC).date() + timedelta(days=days_to_expiry)).isoformat()
                row["expiry_date"] = expiry
                row["batches"] = [
                    {
                        "lot_id": str(props.get("expiry.batch_id") or f"REPORTED-{sku}"),
                        "on_hand": int(row["on_hand"]),
                        "expiry_date": expiry,
                        "received_date": None,
                    }
                ]
        return list(rows.values())

    def list_product_operational_signals(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        signals: dict[str, dict[str, Any]] = {}
        for product in self.list_products(tenant_id):
            sku = str(product["sku"])
            signals[sku] = {
                "supplier": self._supplier_summary(sku),
                "recent_daily_units": self.get_recent_daily_units(tenant_id, sku),
            }
        return signals

    def get_store_intelligence(self, tenant_id: str) -> dict[str, Any]:
        self._require_tenant(tenant_id)
        context = self._twin.live_context(tenant_id, store_id=self._store_id, limit=120)
        return {
            "data_domain": DataDomain.OPERATIONAL_TWIN.value,
            "store_id": self._store_id,
            "reported_property_count": len(context["properties"]),
            "source_refs": context["source_refs"],
            "missing_data": context["missing_data"],
        }

    def _product_for_sku(self, tenant_id: str, sku: str) -> dict[str, Any]:
        for product in self.list_products(tenant_id):
            if product.get("sku") == sku:
                return product
        return {}

    def _properties_for(self, entity_type: str, local_id: str | None) -> dict[str, Any]:
        if not local_id:
            return {}
        target = _slug(local_id)
        valid_ids = {
            entity.twin_id
            for entity in self._twin.store.list_entities(
                self._event.tenant_id,
                store_id=self._store_id,
            )
            if entity.entity_type == entity_type and _local_id(entity.twin_id) == target
        }
        return {
            item.property_name: item.value
            for item in self._twin.store.list_properties(
                self._event.tenant_id,
                store_id=self._store_id,
            )
            if item.twin_id in valid_ids
            and item.lane is StateLane.REPORTED
            and item.freshness is not FreshnessState.STALE
        }

    def _entity_display_name(self, entity_type: str, local_id: str) -> str | None:
        target = _slug(local_id)
        return next(
            (
                entity.display_name
                for entity in self._twin.store.list_entities(
                    self._event.tenant_id,
                    store_id=self._store_id,
                )
                if entity.entity_type == entity_type and _local_id(entity.twin_id) == target
            ),
            None,
        )

    def _inventory_quantity(self, tenant_id: str, sku: str) -> int | None:
        rows = self._inventory.list(tenant_id=tenant_id, sku=sku)
        if self._store_id:
            rows = [row for row in rows if row.get("location_id") == self._store_id]
        return sum(int(row["quantity"]) for row in rows) if rows else None

    def _payload_recent_units(self, payload: dict[str, Any]) -> tuple[Decimal, ...]:
        raw = payload.get("recent_daily_units")
        if not isinstance(raw, list):
            return ()
        values = tuple(_decimal(item) for item in raw[-90:])
        return tuple(item for item in values if item is not None)

    def _supplier_summary(self, sku: str) -> dict[str, Any]:
        try:
            return self.get_supplier_for_sku(self._event.tenant_id, sku)
        except MissingOperationalFacts:
            return {}

    def _require_tenant(self, tenant_id: str) -> None:
        if tenant_id != self._event.tenant_id:
            raise ValueError("facts tenant does not match event tenant")


def _first_text(payload: dict[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = _text(payload.get(field))
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _first_number(payload: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        value = _number(payload.get(field))
        if value is not None:
            return value
    return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None and str(value).strip() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _first_decimal(payload: dict[str, Any], *fields: str) -> Decimal | None:
    for field in fields:
        value = _decimal(payload.get(field))
        if value is not None:
            return value
    return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _first_bool(payload: dict[str, Any], *fields: str) -> bool | None:
    for field in fields:
        value = _bool(payload.get(field))
        if value is not None:
            return value
    return None


def _money_from_fields(
    payload: dict[str, Any],
    *,
    amount_fields: tuple[str, ...],
    minor_fields: tuple[str, ...],
) -> Money | None:
    for field in minor_fields:
        value = _number(payload.get(field))
        if value is not None:
            return Money(minor_units=value)
    for field in amount_fields:
        raw = payload.get(field)
        if isinstance(raw, dict):
            value = _number(raw.get("minor_units"))
            if value is not None:
                return Money(minor_units=value, currency=str(raw.get("currency") or "ZAR"))
        value = _decimal(raw)
        if value is not None:
            return Money.zar(value)
    return None


def _money_from_property(value: Any) -> Money | None:
    minor_units = _number(value)
    return Money(minor_units=minor_units) if minor_units is not None else None


def _local_id(twin_id: str) -> str:
    return twin_id.rsplit(":", 1)[-1]


def _slug(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value).strip())[:120] or "unknown"
