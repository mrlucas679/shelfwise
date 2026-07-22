from __future__ import annotations

import asyncio

from shelfwise_contracts import Money
from shelfwise_resilience.alerts import build_alert, excursion_overlay
from shelfwise_resilience.diagnose import Diagnosis, Severity, Snapshot, diagnose
from shelfwise_resilience.feed import ColdChainFeed
from shelfwise_resilience.ingest import MONNIT_MAP, normalize
from shelfwise_resilience.simulate import SIM_START, FridgeSpec, PowerScenario, simulate_site
from shelfwise_resilience.telemetry import GeneratorState, PowerState, SignalKind, Transport
from shelfwise_resilience.thermal import PROFILES, predict_time_to_unsafe
from shelfwise_resilience.valuation import spoilage_probability, stock_at_risk

CHILLED = PROFILES["chilled"]
FRIDGE = FridgeSpec(
    "fridge_dairy_1",
    "chilled",
    setpoint_c=3.0,
    ambient_c=24.0,
    tau_min=80.0,
    contents_value_c=850_000,
)
FAIL = PowerScenario(outage_at_min=5, restore_at_min=60, generator_succeeds=False)
OK = PowerScenario(outage_at_min=5, restore_at_min=60, generator_succeeds=True)


def _temps(scenario: PowerScenario):
    """Return only temperature readings for the dairy fridge fixture."""
    return [
        reading
        for reading in simulate_site(site_id="s1", fridges=[FRIDGE], scenario=scenario)
        if reading.kind is SignalKind.TEMPERATURE
    ]


def _warming_slice(scenario: PowerScenario):
    """Return the first six post-outage temperature readings."""
    return _readings_from_minute(scenario, minute=5)[:6]


def _readings_from_minute(scenario: PowerScenario, *, minute: int):
    """Return temperature readings from a specific simulated minute onward."""
    return [
        reading
        for reading in _temps(scenario)
        if (reading.ts - SIM_START).total_seconds() / 60 >= minute
    ]


def test_generator_failure_warms_past_safe_but_success_stays_safe():
    assert max(reading.temp_c for reading in _temps(FAIL)) > CHILLED.safe_max_c
    assert max(reading.temp_c for reading in _temps(OK)) < CHILLED.safe_max_c


def test_simulation_is_deterministic_and_labeled():
    first = [
        (reading.asset_id, reading.kind, reading.temp_c)
        for reading in simulate_site(site_id="s1", fridges=[FRIDGE], scenario=FAIL, minutes=30)
    ]
    second = [
        (reading.asset_id, reading.kind, reading.temp_c)
        for reading in simulate_site(site_id="s1", fridges=[FRIDGE], scenario=FAIL, minutes=30)
    ]
    assert first == second
    assert all(reading.synthetic for reading in _temps(FAIL))


def test_predictor_forecasts_unsafe_before_it_happens():
    prediction = predict_time_to_unsafe(_warming_slice(FAIL), profile=CHILLED)
    assert prediction is not None
    assert prediction.slope_c_per_min > 0
    assert 0 < prediction.minutes_to_unsafe < 30
    assert predict_time_to_unsafe(_readings_from_minute(OK, minute=10)[:6], profile=CHILLED) is None


def test_diagnosis_truth_table():
    assert (
        diagnose(Snapshot(PowerState.OUTAGE, GeneratorState.FAILED, 0.26, 0.0)).diagnosis
        is Diagnosis.GENERATOR_FAILED
    )
    assert (
        diagnose(Snapshot(PowerState.OUTAGE, GeneratorState.FAILED, 0.26, 0.0)).severity
        is Severity.CRITICAL
    )
    assert (
        diagnose(Snapshot(PowerState.MAINS, GeneratorState.OFF, 0.2, 0.0)).diagnosis
        is Diagnosis.COMPRESSOR_FAILURE
    )
    assert (
        diagnose(Snapshot(PowerState.MAINS, GeneratorState.OFF, 0.2, 180.0)).diagnosis
        is Diagnosis.UNIT_NOT_COOLING
    )
    assert (
        diagnose(Snapshot(PowerState.MAINS, GeneratorState.OFF, 0.0, 180.0, 15)).diagnosis
        is Diagnosis.DOOR_AJAR
    )
    assert (
        diagnose(Snapshot(PowerState.OUTAGE, GeneratorState.RUNNING, 0.0, 180.0)).diagnosis
        is Diagnosis.ON_GENERATOR
    )
    assert (
        diagnose(Snapshot(PowerState.MAINS, GeneratorState.OFF, 0.0, 180.0)).diagnosis
        is Diagnosis.NORMAL
    )
    assert (
        diagnose(Snapshot(PowerState.OUTAGE, GeneratorState.LOW_FUEL, 0.0, 180.0)).diagnosis
        is Diagnosis.ON_GENERATOR
    )


def test_an_active_warming_trend_is_never_masked_by_a_low_fuel_notice():
    """A live thermal risk to stock must surface even when the generator is also
    reporting low fuel - the fuel notice is informational for later, warming is a
    risk right now. Reproduced live before the fix: this exact snapshot returned
    "on_generator / low fuel" with zero mention of the active warming trend, and
    GeneratorState.LOW_FUEL had no test coverage at all before this pair of tests.
    """
    warming_and_low_fuel = diagnose(
        Snapshot(PowerState.OUTAGE, GeneratorState.LOW_FUEL, 0.3, 180.0)
    )
    assert warming_and_low_fuel.diagnosis is Diagnosis.WARMING
    assert "rising" in warming_and_low_fuel.headline.lower()

    stable_and_low_fuel = diagnose(
        Snapshot(PowerState.OUTAGE, GeneratorState.LOW_FUEL, 0.0, 180.0)
    )
    assert stable_and_low_fuel.diagnosis is Diagnosis.ON_GENERATOR
    assert "fuel" in stable_and_low_fuel.headline.lower()


def test_stock_at_risk_is_money_and_bounded():
    prediction = predict_time_to_unsafe(_warming_slice(FAIL), profile=CHILLED)
    probability = spoilage_probability(prediction, restore_eta_min=25)
    risk = stock_at_risk({"fridge_dairy_1": 850_000}, {"fridge_dairy_1"}, probability)
    assert isinstance(risk, Money)
    assert 0 < risk.minor_units <= 850_000
    assert stock_at_risk({"fridge_dairy_1": 850_000}, set(), 0.5).minor_units == 0


def test_ingest_normalizes_vendor_payload_with_provenance():
    raw = {
        "sensorID": "MN-77",
        "asset_id": "fridge_1",
        "site_id": "s1",
        "temperatureC": 6.4,
        "battery": 9,
        "timestamp": "2026-06-25T02:10:00+00:00",
    }
    reading = normalize(
        raw,
        mapping=MONNIT_MAP,
        kind=SignalKind.TEMPERATURE,
        transport=Transport.LORAWAN,
    )
    assert reading.sensor_id == "MN-77"
    assert reading.temp_c == 6.4
    assert reading.transport is Transport.LORAWAN
    assert reading.quality == 0.5


def test_alert_and_cascade_bridge():
    diagnosis = diagnose(Snapshot(PowerState.OUTAGE, GeneratorState.FAILED, 0.26, 0.0))
    prediction = predict_time_to_unsafe(_warming_slice(FAIL), profile=CHILLED)
    alert = build_alert(
        site_id="s1",
        asset_id="fridge_dairy_1",
        dr=diagnosis,
        pred=prediction,
        stock_at_risk=Money(minor_units=643_500, currency="ZAR"),
        signals=["fridge_dairy_1_t", "s1_gen"],
        ts=SIM_START,
    )
    overlay = excursion_overlay(
        area="observatory_blk7",
        category="dairy",
        measured_outage_hours=4.0,
    )
    assert alert.severity is Severity.CRITICAL
    assert alert.predicted_minutes_to_unsafe is not None
    assert overlay["measured_outage_hours"] == 4.0
    assert overlay["category"] == "dairy"


def test_feed_publishes_fridge_every_tick_but_dedupes_alerts():
    sent: list[tuple[str, dict]] = []

    async def publish(kind: str, data: dict) -> None:
        sent.append((kind, data))

    async def run() -> None:
        feed = ColdChainFeed(
            publish,
            values_c={"fridge_dairy_1": 850_000},
            profiles={"fridge_dairy_1": "chilled"},
        )
        for reading in simulate_site(site_id="s1", fridges=[FRIDGE], scenario=FAIL, minutes=40):
            await feed.ingest(reading)

    asyncio.run(run())
    fridge = [data for kind, data in sent if kind == "fridge"]
    alerts = [data for kind, data in sent if kind == "cold_chain"]
    assert len(fridge) == 40
    assert fridge[0]["status"] == "safe"
    assert fridge[-1]["status"] in {"at_risk", "unsafe"}
    assert len(fridge[-1]["trend"]) >= 2
    assert fridge[-1]["stock_at_risk"]["minor_units"] > 0
    assert 1 <= len(alerts) <= 2
    assert alerts[0]["diagnosis"] == "generator_failed"
    assert alerts[0]["severity"] == 2


def test_feed_recovery_rearms_the_alert():
    sent: list[tuple[str, dict]] = []

    async def publish(kind: str, data: dict) -> None:
        sent.append((kind, data))

    async def run() -> None:
        feed = ColdChainFeed(
            publish,
            values_c={"fridge_dairy_1": 850_000},
            profiles={"fridge_dairy_1": "chilled"},
        )
        short = PowerScenario(outage_at_min=5, restore_at_min=12, generator_succeeds=False)
        for _ in range(2):
            for reading in simulate_site(
                site_id="s1",
                fridges=[FRIDGE],
                scenario=short,
                minutes=40,
            ):
                await feed.ingest(reading)

    asyncio.run(run())
    failures = [
        data
        for kind, data in sent
        if kind == "cold_chain" and data["diagnosis"] == "generator_failed"
    ]
    assert len(failures) == 2
