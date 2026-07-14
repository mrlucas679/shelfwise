from datetime import UTC, datetime

import pytest

from shelfwise_twin import (
    InMemoryScenarioBranchStore,
    InMemoryTwinStore,
    ScenarioDelta,
    ScenarioEngine,
    ScenarioRequest,
    StateLane,
    TwinEntity,
    TwinObservation,
    TwinService,
)


def _service() -> TwinService:
    service = TwinService(InMemoryTwinStore())
    service.store.ensure_entity(TwinEntity(
        twin_id="urn:shelfwise:t:s:product:milk", tenant_id="t", store_id="s",
        entity_type="product", display_name="Milk",
    ))
    service.accept(TwinObservation(
        observation_id="obs_reported_milk", tenant_id="t", store_id="s",
        twin_id="urn:shelfwise:t:s:product:milk", property_name="inventory.stock",
        lane=StateLane.REPORTED, value=10, observed_at=datetime.now(UTC),
        source_system="pos", source_object_id="sale-1", source_quality=1,
        correlation_id="c", payload_hash="a" * 64,
    ))
    return service


def test_scenario_isolated_from_reported_state() -> None:
    service = _service()
    result = ScenarioEngine(service).create("t", "s", ScenarioRequest(
        branch_id="replenish", deltas=[ScenarioDelta(
            twin_id="urn:shelfwise:t:s:product:milk", property_name="inventory.stock", value=30,
        )],
    ))
    assert result["predicted_count"] == 1
    assert result["reported_state_untouched"] is True
    properties = service.store.list_properties("t", store_id="s")
    assert any(item.lane is StateLane.REPORTED and item.value == 10 for item in properties)
    assert any(item.lane is StateLane.PREDICTED and item.value == 30 for item in properties)


def test_scenario_rejects_unknown_entity() -> None:
    with pytest.raises(ValueError, match="unknown store entity"):
        ScenarioEngine(_service()).create("t", "s", ScenarioRequest(
            branch_id="bad", deltas=[ScenarioDelta(
                twin_id="urn:shelfwise:t:s:product:none", property_name="inventory.stock", value=2,
            )],
        ))


def test_scenario_metadata_survives_engine_restart() -> None:
    service = _service()
    branches = InMemoryScenarioBranchStore()
    ScenarioEngine(service, branches).create(
        "t",
        "s",
        ScenarioRequest(
            branch_id="restart-proof",
            deltas=[
                ScenarioDelta(
                    twin_id="urn:shelfwise:t:s:product:milk",
                    property_name="inventory.stock",
                    value=22,
                )
            ],
        ),
    )

    result = ScenarioEngine(service, branches).compare("t", "s", "restart-proof")

    assert result["predicted_count"] == 1
    assert result["rows"][0]["predicted"] == 22
    assert result["reported_state_untouched"] is True
