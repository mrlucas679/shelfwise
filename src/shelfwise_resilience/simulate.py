from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .telemetry import (
    GeneratorState,
    Medium,
    PowerState,
    SensorReading,
    SignalKind,
    Transport,
)

SIM_START = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FridgeSpec:
    asset_id: str
    profile: str
    setpoint_c: float
    ambient_c: float
    tau_min: float
    contents_value_c: int
    medium: Medium = Medium.AIR


@dataclass(frozen=True, slots=True)
class PowerScenario:
    outage_at_min: int
    restore_at_min: int
    generator_succeeds: bool
    gen_start_delay_min: int = 2


DEMO_FRIDGES = [
    FridgeSpec(
        "fridge_dairy_1",
        "chilled",
        setpoint_c=3.0,
        ambient_c=24.0,
        tau_min=80.0,
        contents_value_c=850_000,
    ),
    FridgeSpec(
        "fridge_meat_1",
        "chilled",
        setpoint_c=2.0,
        ambient_c=24.0,
        tau_min=110.0,
        contents_value_c=620_000,
    ),
    FridgeSpec(
        "freezer_1",
        "frozen",
        setpoint_c=-20.0,
        ambient_c=24.0,
        tau_min=240.0,
        contents_value_c=910_000,
    ),
]
DEMO_SCENARIO = PowerScenario(outage_at_min=5, restore_at_min=45, generator_succeeds=False)


def simulate_site(
    *,
    site_id: str,
    fridges: list[FridgeSpec],
    scenario: PowerScenario,
    minutes: int = 60,
    step: int = 1,
) -> Iterator[SensorReading]:
    """Emit a deterministic outage and generator telemetry stream."""
    temps = {fridge.asset_id: fridge.setpoint_c for fridge in fridges}
    for minute in range(0, minutes, step):
        ts = SIM_START + timedelta(minutes=minute)
        mains = not (scenario.outage_at_min <= minute < scenario.restore_at_min)
        generator = _generator(scenario, minute)
        cooling = mains or generator is GeneratorState.RUNNING
        yield SensorReading(
            sensor_id=f"{site_id}_mains",
            asset_id=site_id,
            site_id=site_id,
            kind=SignalKind.POWER,
            ts=ts,
            power=PowerState.MAINS if mains else PowerState.OUTAGE,
            transport=Transport.GATEWAY,
            synthetic=True,
        )
        yield SensorReading(
            sensor_id=f"{site_id}_gen",
            asset_id=site_id,
            site_id=site_id,
            kind=SignalKind.GENERATOR,
            ts=ts,
            generator=generator,
            transport=Transport.GATEWAY,
            synthetic=True,
        )
        for fridge in fridges:
            target = fridge.setpoint_c if cooling else fridge.ambient_c
            temps[fridge.asset_id] = target + (
                temps[fridge.asset_id] - target
            ) * math.exp(-step / fridge.tau_min)
            yield SensorReading(
                sensor_id=f"{fridge.asset_id}_t",
                asset_id=fridge.asset_id,
                site_id=site_id,
                kind=SignalKind.TEMPERATURE,
                ts=ts,
                temp_c=round(temps[fridge.asset_id], 3),
                medium=fridge.medium,
                transport=Transport.LORAWAN,
                battery_pct=88.0,
                synthetic=True,
            )
            yield SensorReading(
                sensor_id=f"{fridge.asset_id}_e",
                asset_id=fridge.asset_id,
                site_id=site_id,
                kind=SignalKind.ENERGY,
                ts=ts,
                energy_w=180.0 if cooling else 0.0,
                transport=Transport.LORAWAN,
                synthetic=True,
            )


def _generator(scenario: PowerScenario, minute: int) -> GeneratorState:
    """Resolve generator state for the current simulated minute."""
    if not (scenario.outage_at_min <= minute < scenario.restore_at_min):
        return GeneratorState.OFF
    if not scenario.generator_succeeds:
        return GeneratorState.FAILED
    if minute - scenario.outage_at_min < scenario.gen_start_delay_min:
        return GeneratorState.STARTING
    return GeneratorState.RUNNING
