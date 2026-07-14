from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from shelfwise_twin import StateLane, TwinObservation


def _observation(**overrides: object) -> TwinObservation:
    """Build a valid reported observation for contract tests."""
    value = overrides.pop("value", 4)
    digest = sha256(f"{value}".encode()).hexdigest()
    payload: dict[str, object] = {
        "observation_id": "obs_contract_001",
        "tenant_id": "tenant_a",
        "store_id": "store_1",
        "twin_id": "urn:shelfwise:tenant_a:store_1:product:sku1",
        "property_name": "inventory.on_hand",
        "lane": StateLane.REPORTED,
        "value": value,
        "observed_at": datetime.now(UTC),
        "source_system": "wms_csv",
        "source_object_id": "evt_contract_001",
        "source_quality": 1.0,
        "correlation_id": "cor_contract_001",
        "payload_hash": digest,
    }
    payload.update(overrides)
    return TwinObservation.model_validate(payload)


def test_observation_requires_branch_for_predictions() -> None:
    with pytest.raises(ValidationError, match="scenario_branch_id"):
        _observation(lane=StateLane.PREDICTED)


def test_observation_rejects_raw_media_fields() -> None:
    with pytest.raises(ValidationError, match="raw media"):
        _observation(value={"frame": "base64-content"})


def test_observation_accepts_isolated_predicted_branch() -> None:
    observation = _observation(
        lane=StateLane.PREDICTED,
        scenario_branch_id="scenario_loadshedding_1",
    )
    assert observation.lane is StateLane.PREDICTED
    assert observation.scenario_branch_id == "scenario_loadshedding_1"
