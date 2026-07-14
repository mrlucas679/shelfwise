from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from shelfwise_twin import (
    InMemoryTwinStore,
    StateLane,
    TwinObservation,
    TwinProjector,
    TwinService,
)


def _observation(
    observation_id: str,
    value: int,
    observed_at: datetime,
    *,
    source_system: str = "wms_csv",
    lane: StateLane = StateLane.REPORTED,
    scenario_branch_id: str | None = None,
) -> TwinObservation:
    """Build one deterministic observation with a unique source hash."""
    digest = sha256(f"{observation_id}|{value}".encode()).hexdigest()
    return TwinObservation(
        observation_id=observation_id,
        tenant_id="tenant_a",
        store_id="store_1",
        twin_id="urn:shelfwise:tenant_a:store_1:product:sku1",
        property_name="inventory.on_hand",
        lane=lane,
        value=value,
        observed_at=observed_at,
        source_system=source_system,
        source_object_id=observation_id,
        source_quality=1.0,
        correlation_id="correlation_1",
        scenario_branch_id=scenario_branch_id,
        payload_hash=digest,
    )


def test_projector_is_idempotent_and_rejects_stale_state() -> None:
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    store = InMemoryTwinStore()
    projector = TwinProjector(store, clock=lambda: now)
    first = _observation("obs_projector_1", 10, now - timedelta(minutes=2))
    stale = _observation("obs_projector_2", 2, now - timedelta(minutes=5))

    assert projector.apply(first).status == "projected"
    assert projector.apply(first).status == "duplicate"
    stale_result = projector.apply(stale)
    assert stale_result.status == "recorded_not_projected"
    assert stale_result.state is not None and stale_result.state.value == 10


def test_projector_keeps_reported_and_predicted_lanes_separate() -> None:
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    store = InMemoryTwinStore()
    projector = TwinProjector(store, clock=lambda: now)
    reported = _observation("obs_reported_1", 10, now)
    predicted = _observation(
        "obs_predicted_1",
        3,
        now,
        lane=StateLane.PREDICTED,
        scenario_branch_id="scenario_1",
    )

    assert projector.apply(reported).status == "projected"
    assert projector.apply(predicted).status == "projected"
    assert store.get_property(
        tenant_id="tenant_a",
        twin_id=reported.twin_id,
        property_name=reported.property_name,
        lane=StateLane.REPORTED,
        scenario_branch_id=None,
    ).value == 10
    assert store.get_property(
        tenant_id="tenant_a",
        twin_id=predicted.twin_id,
        property_name=predicted.property_name,
        lane=StateLane.PREDICTED,
        scenario_branch_id="scenario_1",
    ).value == 3


def test_projector_exposes_stale_freshness_without_deleting_history() -> None:
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    store = InMemoryTwinStore()
    projector = TwinProjector(store, clock=lambda: now)
    result = projector.apply(_observation("obs_old_1", 5, now - timedelta(days=4)))

    assert result.state is not None
    assert result.state.freshness.value == "stale"
    assert len(store.list_observations("tenant_a")) == 1


def test_live_context_excludes_predicted_scenario_state() -> None:
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    service = TwinService(InMemoryTwinStore())
    service.accept(_observation("obs_reported_fridge", 25, now))
    service.accept(
        _observation(
            "obs_predicted_fridge",
            11,
            now,
            lane=StateLane.PREDICTED,
            scenario_branch_id="digital-twin-what-if",
        )
    )

    context = service.live_context("tenant_a", store_id="store_1")

    assert context["data_domain"] == "operational_twin"
    assert context["synthetic"] is False
    assert [row["value"] for row in context["properties"]] == [25]
    assert all(row["lane"] == "reported" for row in context["properties"])
