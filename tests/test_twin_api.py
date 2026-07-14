from __future__ import annotations

from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.app import app


def test_canonical_event_updates_exact_store_twin_without_replacing_cascade() -> None:
    client = TestClient(app)
    event = {
        "id": "evt_twin_api_001",
        "type": "stock_update",
        "ts": "2026-07-13T08:00:00Z",
        "actor": "store_12",
        "source": "wms_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "store_id": "store_12",
            "sku": "SKU-TWIN-1",
            "product": "Twin Test Product",
            "on_hand": 18,
            "reorder_point": 6,
        },
    }

    response = client.post("/ingest", json=event)
    assert response.status_code == 200
    assert response.json()["twin"]["status"] == "projected"

    store = client.get("/twin/stores/store_12")
    assert store.status_code == 200
    body = store.json()
    assert body["entities"]
    assert {item["property_name"] for item in body["properties"]} == {
        "inventory.on_hand",
        "inventory.reorder_point",
    }
    assert body["fidelity"]["hard_guards"]["no_raw_media"] is True


def test_twin_observation_intake_is_tenant_bound_and_idempotent() -> None:
    client = TestClient(app)
    body = {
        "observation_id": "obs_api_001",
        "tenant_id": "sa_retail_demo",
        "store_id": "store_12",
        "twin_id": "urn:shelfwise:sa_retail_demo:store_12:fixture:fridge_1",
        "property_name": "cold_chain.status",
        "lane": "reported",
        "value": "healthy",
        "observed_at": "2026-07-13T08:00:00Z",
        "source_system": "api",
        "source_object_id": "device-event-001",
        "source_quality": 1.0,
        "correlation_id": "cor-api-001",
        "payload_hash": "a" * 64,
    }

    first = client.post("/twin/observations", json=body)
    duplicate = client.post("/twin/observations", json=body)
    other_tenant = client.post(
        "/twin/observations",
        json={**body, "observation_id": "obs_api_002", "tenant_id": "another_tenant"},
    )

    assert first.status_code == 200
    assert first.json()["result"]["status"] == "projected"
    assert duplicate.status_code == 200
    assert duplicate.json()["result"]["status"] == "duplicate"
    assert other_tenant.status_code == 403


def test_onboarding_bootstrap_and_snapshot_are_replay_safe() -> None:
    client = TestClient(app)
    manifest = {
        "tenant_id": "sa_retail_demo",
        "store_id": "exact_store_1",
        "display_name": "Johannesburg Observatory Shop",
        "timezone": "Africa/Johannesburg",
        "entities": [
            {
                "local_id": "fridge_1",
                "entity_type": "fixture",
                "display_name": "Dairy Fridge 1",
                "attributes": {"zone": "dairy"},
            }
        ],
    }
    onboard = client.post("/twin/onboarding", json=manifest)
    assert onboard.status_code == 200
    assert onboard.json()["snapshot"]["entity_count"] == 2

    event = {
        "id": "evt_twin_bootstrap_001",
        "type": "stock_update",
        "ts": "2026-07-13T08:00:00Z",
        "actor": "exact_store_1",
        "source": "wms_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "store_id": "exact_store_1",
            "sku": "SKU-BOOTSTRAP-1",
            "on_hand": 11,
        },
    }
    assert client.post("/ingest", json=event).status_code == 200
    simulation = {
        **event,
        "id": "evt_twin_bootstrap_simulation_001",
        "data_domain": "world_simulation",
        "payload": {**event["payload"], "on_hand": 999, "synthetic": True},
    }
    simulation_result = client.post("/ingest", json=simulation)
    assert simulation_result.status_code == 200
    assert simulation_result.json()["twin"]["status"] == "skipped_non_operational"

    hash_before_restart = client.get(
        "/twin/stores/exact_store_1/snapshot"
    ).json()["projection_hash"]

    # Simulate total loss of the projected twin state (the scenario /bootstrap exists to
    # recover from). Onboarding writes straight to the twin store, not the durable event log,
    # so this also exercises the onboarding-manifest registry's replay path, not just events.
    app_module.twin_service.store.clear()

    replay = client.post("/twin/stores/exact_store_1/bootstrap")
    assert replay.status_code == 200
    assert replay.json()["events_considered"] == 1
    assert replay.json()["events_skipped_non_operational"] == 0
    assert replay.json()["projected"] == 1
    snapshot = client.get("/twin/stores/exact_store_1/snapshot")
    assert snapshot.status_code == 200
    assert len(snapshot.json()["projection_hash"]) == 64
    assert snapshot.json()["projection_hash"] == hash_before_restart

    entity = client.get(
        "/twin/entities/urn:shelfwise:sa_retail_demo:exact_store_1:product:SKU-BOOTSTRAP-1"
    )
    assert entity.status_code == 200

    # The onboarded fixture and the store's onboarded display name/attributes must survive
    # the simulated restart, not just the event-sourced product entity.
    fixture = client.get(
        "/twin/entities/urn:shelfwise:sa_retail_demo:exact_store_1:fixture:fridge_1"
    )
    assert fixture.status_code == 200
    assert fixture.json()["entity"]["display_name"] == "Dairy Fridge 1"
    assert fixture.json()["entity"]["attributes"] == {"zone": "dairy"}

    store_entity = client.get(
        "/twin/entities/urn:shelfwise:sa_retail_demo:exact_store_1:store:exact_store_1"
    )
    assert store_entity.status_code == 200
    assert store_entity.json()["entity"]["display_name"] == "Johannesburg Observatory Shop"
    assert store_entity.json()["entity"]["attributes"] == {
        "timezone": "Africa/Johannesburg",
        "onboarding": "explicit",
    }


def test_event_sourced_projection_hash_is_stable_across_a_simulated_restart() -> None:
    """Rebuilding purely from the durable event log must reproduce the same projection hash."""
    client = TestClient(app)
    event = {
        "id": "evt_twin_restart_001",
        "type": "stock_update",
        "ts": "2026-07-13T08:00:00Z",
        "actor": "restart_store_1",
        "source": "wms_csv",
        "tenant_id": "sa_retail_demo",
        "payload": {
            "store_id": "restart_store_1",
            "sku": "SKU-RESTART-1",
            "on_hand": 7,
        },
    }
    assert client.post("/ingest", json=event).status_code == 200

    hash_before_restart = client.get(
        "/twin/stores/restart_store_1/snapshot"
    ).json()["projection_hash"]

    # Simulate a process restart: wipe the projected twin state (as a fresh process would
    # start with an empty in-memory/Postgres projection) and rebuild it purely by replaying
    # the durable event log.
    app_module.twin_service.store.clear()

    replay = client.post("/twin/stores/restart_store_1/bootstrap")
    assert replay.status_code == 200
    assert replay.json()["events_considered"] == 1
    assert replay.json()["projected"] == 1

    hash_after_restart = client.get(
        "/twin/stores/restart_store_1/snapshot"
    ).json()["projection_hash"]
    assert hash_after_restart == hash_before_restart
