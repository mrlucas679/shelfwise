from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.app import app
from shelfwise_backend.tenant import default_tenant_context
from shelfwise_catalog import Product, ProductIdentifier, ProductVariant
from shelfwise_twin import StateLane, TwinEntity, TwinObservation

_TENANT = default_tenant_context().tenant_id
_SKU = "SKU-OPS-AGENTIC-1"
_SUPPLIER = "SUP-OPS-AGENTIC-1"
_STORE_ID = "store_ops_agentic"


def _entity(entity_type: str, local_id: str, display_name: str) -> TwinEntity:
    return TwinEntity(
        twin_id=f"urn:shelfwise:{_TENANT}:{_STORE_ID}:{entity_type}:{local_id}",
        tenant_id=_TENANT,
        store_id=_STORE_ID,
        entity_type=entity_type,
        display_name=display_name,
        model_version="test-v1",
        created_at=datetime(2026, 7, 6, 8, tzinfo=UTC),
    )


def _observation(
    twin_id: str, property_name: str, value: object, *, unit: str | None = None
) -> TwinObservation:
    digest = hashlib.sha256(f"{twin_id}:{property_name}:{value}".encode()).hexdigest()
    return TwinObservation(
        observation_id=f"obs_{digest[:24]}",
        tenant_id=_TENANT,
        store_id=_STORE_ID,
        twin_id=twin_id,
        property_name=property_name,
        lane=StateLane.REPORTED,
        value=value,
        unit=unit,
        observed_at=datetime.now(UTC),
        source_system="test_fixture",
        source_object_id=f"fixture:{property_name}",
        source_quality=1.0,
        correlation_id="test_agentic_operational_twin",
        payload_hash=digest,
    )


def _seed_catalog_and_twin() -> None:
    """Register one product/variant/sku and directly seed the reported twin properties
    OperationalFactsProvider.get_scenario_facts needs - bypassing event-type-to-property
    mapping details (STOCK_UPDATE/SALE/EXPIRY_ENTRY/SUPPLIER_UPDATE each project a different
    subset) so this test asserts the agentic route's facts wiring, not the projector's event
    vocabulary.
    """
    app_module.product_catalog_store.upsert_product(
        Product(
            tenant_id=_TENANT,
            product_id="prod_ops_1",
            name="Measured Milk 1L",
            category="dairy",
        )
    )
    app_module.product_catalog_store.upsert_variant(
        ProductVariant(tenant_id=_TENANT, variant_id="var_ops_1", product_id="prod_ops_1")
    )
    app_module.product_catalog_store.upsert_identifier(
        ProductIdentifier(tenant_id=_TENANT, variant_id="var_ops_1", kind="sku", value=_SKU)
    )

    product_entity = app_module.twin_service.store.ensure_entity(
        _entity("product", _SKU, "Measured Milk 1L")
    )
    supplier_entity = app_module.twin_service.store.ensure_entity(
        _entity("supplier", _SUPPLIER, "Measured Supplies Ltd")
    )

    for property_name, value, unit in (
        ("catalog.category", "dairy", None),
        ("catalog.unit_cost_minor_units", 1250, "ZAR cents"),
        ("catalog.unit_price_minor_units", 2000, "ZAR cents"),
        ("sourcing.supplier_id", _SUPPLIER, None),
        ("inventory.on_hand", 40, "units"),
        ("inventory.reorder_point", 20, "units"),
        ("expiry.days_to_expiry", 2, "days"),
        ("sales.units", 8, "units"),
        ("sales.units", 9, "units"),
        ("sales.units", 10, "units"),
        ("sales.units", 11, "units"),
        ("sales.units", 12, "units"),
    ):
        app_module.twin_service.accept(
            _observation(product_entity.twin_id, property_name, value, unit=unit)
        )

    for property_name, value, unit in (
        ("supplier.lead_time_days", 2, "days"),
        ("supplier.recent_delay", False, None),
        ("supplier.fill_rate", 0.95, None),
    ):
        app_module.twin_service.accept(
            _observation(supplier_entity.twin_id, property_name, value, unit=unit)
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app_module.twin_service.store.clear()
    app_module.decision_store.clear()
    app_module.event_store.clear()


def test_golden_agentic_route_resolves_operational_twin_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/scenarios/golden/agentic?data_domain=operational_twin` must ground the Critic/Executive
    tool-calling loop in reported twin state, not the generated world - the central gap this
    fix closes."""
    _seed_catalog_and_twin()
    captured: dict[str, Any] = {}

    def spy(event, **kwargs):
        captured["event"] = event
        captured["facts"] = kwargs.get("facts")
        return {
            "agentic": True,
            "decision": None,
            "tenant_id": event.tenant_id,
            "data_domain": event.data_domain.value,
        }

    monkeypatch.setattr(app_module, "run_golden_cascade_via_agents", spy)

    client = TestClient(app)
    response = client.post(
        "/scenarios/golden/agentic",
        params={"data_domain": "operational_twin", "store_id": _STORE_ID},
    )

    assert response.status_code == 200, response.text
    facts = captured["facts"]
    assert facts is not app_module.world_facts
    scenario = facts.get_scenario_facts(captured["event"].tenant_id)
    assert scenario.sku == _SKU
    assert scenario.units_on_hand == 40
    assert captured["event"].data_domain.value == "operational_twin"


def test_golden_agentic_route_422s_when_twin_has_no_measured_facts() -> None:
    """A store with no onboarded twin/catalog data must fail closed with a clear error, not
    a 500 or a silent fall-back to synthetic world facts."""
    client = TestClient(app)

    response = client.post(
        "/scenarios/golden/agentic",
        params={"data_domain": "operational_twin", "store_id": "store_never_onboarded"},
    )

    assert response.status_code == 422
    assert "missing operational facts" in response.json()["detail"]
