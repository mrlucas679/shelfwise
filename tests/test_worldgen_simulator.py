from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_contracts import EventType
from shelfwise_worldgen.catalog.sample import sample_assortment
from shelfwise_worldgen.drills import run_drill
from shelfwise_worldgen.narrate import narrate
from shelfwise_worldgen.play import direct
from shelfwise_worldgen.sa_ground_truth import demand_multiplier, load_shedding_schedule
from shelfwise_worldgen.scenarios import SCENARIOS, build
from shelfwise_worldgen.seed import build_memory_seed
from shelfwise_worldgen.store import InMemoryWorldgenRunStore
from shelfwise_worldgen.world import World, WorldConfig


def test_world_emits_only_canonical_valid_events():
    events = list(World(WorldConfig(seed=1, days=2)).run())
    assert events
    assert {event.type for event in events} <= set(EventType)
    assert all(event.tenant_id == "sa_retail_demo" for event in events)
    assert all(event.to_dict()["type"] in {item.value for item in EventType} for event in events)


def test_world_is_deterministic():
    first = [event.to_dict() for event in World(WorldConfig(seed=3, days=2)).run()]
    second = [event.to_dict() for event in World(WorldConfig(seed=3, days=2)).run()]
    assert first == second


def test_load_shedding_never_touches_the_event_stream():
    events = list(World(WorldConfig(seed=4, days=2)).run())
    for event in events:
        payload_text = str(event.payload).lower()
        assert "load_shedding" not in payload_text
        assert "stage" not in event.payload


def test_world_never_emits_an_answer_field():
    events = list(World(WorldConfig(seed=4, days=2)).run())
    forbidden = {"answer", "recommendation", "collapse", "action"}
    assert all(forbidden.isdisjoint(event.payload) for event in events)


def test_incident_window_has_refrigerated_batches_to_reason_about():
    world, schedule = build("stage4_payday_coldchain")
    events = list(world.run())
    refrigerated = [
        event
        for event in events
        if event.type is EventType.EXPIRY_ENTRY
        and event.payload["storage"] in {"chilled", "frozen"}
    ]
    assert schedule
    assert refrigerated


def test_reseed_changes_the_week():
    first, _schedule = build("stage4_payday_coldchain", seed_override=1)
    second, _schedule = build("stage4_payday_coldchain", seed_override=2)
    assert [event.to_dict() for event in first.run()] != [event.to_dict() for event in second.run()]


def test_payday_lifts_demand():
    assert demand_multiplier(SCENARIOS["stage4_payday_coldchain"].start.replace(day=25)) > (
        demand_multiplier(SCENARIOS["stage4_payday_coldchain"].start.replace(day=12))
    )


def test_load_shedding_schedule_is_labeled_and_present():
    scenario = SCENARIOS["stage4_payday_coldchain"]
    schedule = load_shedding_schedule(
        scenario.seed,
        area=scenario.area,
        start=scenario.start,
        days=scenario.days,
        stage=scenario.stage,
    )
    assert schedule
    assert all(row["synthetic"] is True for row in schedule)
    assert {row["stage"] for row in schedule} == {4}


def test_scenarios_carry_no_answer():
    scenario = SCENARIOS["stage4_payday_coldchain"]
    assert not hasattr(scenario, "answer")
    assert not hasattr(scenario, "expected_decision")


def test_assortment_size_swaps_in_the_full_generated_catalog():
    world, _schedule = build("stage4_payday_coldchain", assortment_size=500)
    assert len(world.products) == 500
    departments = {product.department for product in world.products}
    assert len(departments) > 5, "a real assortment should span many departments"


def test_default_build_keeps_the_small_ground_truth_set():
    world, _schedule = build("stage4_payday_coldchain")
    assert len(world.products) == 6


def test_assortment_is_deterministic_for_the_same_seed():
    first, _schedule = build("stage4_payday_coldchain", seed_override=7, assortment_size=200)
    second, _schedule = build("stage4_payday_coldchain", seed_override=7, assortment_size=200)
    assert [p.product_id for p in first.products] == [p.product_id for p in second.products]


def test_world_accepts_a_catalog_assortment():
    assortment = sample_assortment(9, size=80)
    events = list(World(WorldConfig(seed=9, days=1, products=assortment)).run())
    assert events
    assert {event.payload["sku"] for event in events if "sku" in event.payload}


def test_sales_carry_catalog_reference_price():
    events = list(World(WorldConfig(seed=5, days=2)).run())
    sales = [event for event in events if event.type is EventType.SALE]
    assert sales
    for sale in sales:
        assert int(sale.payload["catalog_price_cents"]) > 0
        assert int(sale.payload["unit_price_cents"]) > 0


def test_mispricing_is_rare_and_deterministic():
    assortment = sample_assortment(11, size=400)
    config = WorldConfig(seed=11, days=3, products=assortment)

    def _outlier_count(events) -> tuple[int, int]:
        sales = [event for event in events if event.type is EventType.SALE]
        outliers = 0
        for sale in sales:
            observed = int(sale.payload["unit_price_cents"])
            catalog = int(sale.payload["catalog_price_cents"])
            if abs(observed - catalog) > catalog * 0.15:
                outliers += 1
        return outliers, len(sales)

    first_outliers, first_sales = _outlier_count(World(config).run())
    second_outliers, _ = _outlier_count(World(config).run())

    assert first_outliers == second_outliers, "mispricing must be seed-deterministic"
    assert first_outliers > 0, "some sales should be genuinely mispriced"
    assert first_outliers < first_sales * 0.06, "mispricing should stay rare"


def test_memory_seed_covers_catalog_with_cold_chain_join_keys():
    assortment = sample_assortment(9, size=80)
    seed = build_memory_seed(WorldConfig(seed=9, products=assortment))
    assert seed
    assert all("physics" in row and "storage" in row for row in seed.values())
    assert any(row["refrigerated"] for row in seed.values())


def test_director_drives_run_one_in_timestamp_order():
    seen = []

    async def run_one(event, context):
        seen.append((event.ts, context["scenario_id"]))
        return []

    frames = asyncio.run(direct("stage4_payday_coldchain", run_one=run_one))
    assert frames
    assert [timestamp for timestamp, _scenario in seen] == sorted(
        timestamp for timestamp, _scenario in seen
    )


def test_drill_aggregates_the_week_into_one_report():
    async def run_one(event, context):
        _ = event, context
        return [{"status": "pending", "outcome": {"rand_recovered": {"minor_units": 100}}}]

    report = asyncio.run(run_drill("stage4_payday_coldchain", run_one=run_one))
    assert report.events_total > 0
    assert report.decisions_total == report.pending_total
    assert report.recovered_cents == report.decisions_total * 100
    assert "stage4_payday_coldchain" in report.summary()


def test_narration_is_offline_safe_and_labeled():
    async def run_one(event, context):
        _ = event, context
        return []

    frames = asyncio.run(direct("stage4_payday_coldchain", run_one=run_one))

    async def broken_llm(prompt: str) -> str:
        _ = prompt
        raise RuntimeError("offline")

    text = asyncio.run(narrate(frames[0], headline="Synthetic drill", llm=broken_llm))
    assert text.startswith("Synthetic drill")


def test_worldgen_demo_drives_real_backend_pipeline():
    client = TestClient(app)

    response = client.get("/demo/worldgen/stage4_payday_coldchain?limit=12")

    assert response.status_code == 200
    body = response.json()
    run = body["run"]
    assert run["run_id"].startswith("worldrun_")
    assert run["scenario_id"] == "stage4_payday_coldchain"
    assert run["tenant_id"] == "sa_retail_demo"
    assert run["events_total"] == 13
    assert run["decisions_total"] == len(body["decisions"])
    assert body["synthetic"] is True
    assert body["events_total"] == 13
    assert body["events_accepted"] == 13
    assert body["schedule_sample"]
    assert any(event["type"] == "stock_update" for event in body["events"])
    assert body["decisions"]
    assert any(decision["role"] == "sales_manager" for decision in body["decisions"])
    facilities = next(
        decision for decision in body["decisions"] if decision["role"] == "facilities_manager"
    )
    assert facilities["action"]["type"] == "dispatch_facilities_check"
    assert facilities["expected_outcome"]["stock_at_risk_minor_units"] == 643500

    events = client.get("/events?limit=50").json()["events"]
    assert any(event["type"] == "cold_chain_alert" for event in events)

    runs_response = client.get("/demo/worldgen-runs")
    assert runs_response.status_code == 200
    runs = runs_response.json()["runs"]
    assert [item["run_id"] for item in runs] == [run["run_id"]]

    get_response = client.get(f"/demo/worldgen-runs/{run['run_id']}")
    assert get_response.status_code == 200
    assert get_response.json()["run"]["event_ids"] == run["event_ids"]


def test_worldgen_demo_rejects_unknown_scenario():
    client = TestClient(app)

    response = client.get("/demo/worldgen/not-a-scenario")

    assert response.status_code == 404


def test_worldgen_demo_can_sweep_the_full_generated_catalog():
    client = TestClient(app)

    response = client.get(
        "/demo/worldgen/stage4_payday_coldchain"
        "?limit=50&assortment_size=300&seed_override=99"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["events_total"] > 0

    events = client.get("/events?limit=200").json()["events"]
    skus = {event["payload"].get("sku") for event in events if "sku" in event.get("payload", {})}
    assert len(skus) > 5, "a 300-product assortment should surface many distinct SKUs"


def test_worldgen_demo_rejects_out_of_range_assortment_size():
    client = TestClient(app)

    response = client.get("/demo/worldgen/stage4_payday_coldchain?assortment_size=0")

    assert response.status_code == 422


def test_worldgen_demo_rejects_unknown_catalog_scale():
    client = TestClient(app)

    response = client.get(
        "/demo/worldgen/stage4_payday_coldchain?assortment_size=50&catalog_scale=not-a-scale"
    )

    assert response.status_code == 422


def test_worldgen_run_store_filters_by_tenant_and_validates_limit():
    store = InMemoryWorldgenRunStore()
    first = store.record(
        {
            "run_id": "run_1",
            "tenant_id": "tenant_1",
            "scenario_id": "stage4_payday_coldchain",
            "seed": 42,
            "status": "completed",
            "events_total": 1,
        }
    )
    store.record(
        {
            "run_id": "run_2",
            "tenant_id": "tenant_2",
            "scenario_id": "stage4_payday_coldchain",
            "seed": 7,
            "status": "completed",
            "events_total": 1,
        }
    )

    assert store.get("run_1") == first
    assert [run["run_id"] for run in store.list(tenant_id="tenant_1")] == ["run_1"]
    assert [run["run_id"] for run in store.list(tenant_id="tenant_2")] == ["run_2"]
    try:
        store.list(limit=0)
    except ValueError as exc:
        assert "limit must be between 1 and 500" in str(exc)
    else:
        raise AssertionError("expected limit validation")
