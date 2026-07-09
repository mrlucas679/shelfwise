from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable

from .alerts import build_alert
from .diagnose import Diagnosis, Severity, Snapshot, diagnose
from .simulate import DEMO_FRIDGES, DEMO_SCENARIO, FridgeSpec, PowerScenario, simulate_site
from .telemetry import GeneratorState, PowerState, SensorReading, SignalKind
from .thermal import PROFILES, predict_time_to_unsafe
from .valuation import spoilage_probability, stock_at_risk

Publish = Callable[[str, dict], Awaitable[None]]

_TREND_LEN = 24
_RESTORE_ETA_MIN = 25.0


class ColdChainFeed:
    """Reduce telemetry into fridge status ticks and deduplicated alerts."""

    def __init__(
        self,
        publish: Publish,
        *,
        values_c: dict[str, int],
        profiles: dict[str, str],
        restore_eta_min: float = _RESTORE_ETA_MIN,
    ) -> None:
        self._publish = publish
        self._values_c = values_c
        self._profiles = profiles
        self._eta = restore_eta_min
        self._window: dict[str, deque[SensorReading]] = defaultdict(
            lambda: deque(maxlen=_TREND_LEN)
        )
        self._power = PowerState.MAINS
        self._generator = GeneratorState.OFF
        self._energy: dict[str, float] = {}
        self._door_min: dict[str, float] = {}
        self._last_alert: dict[str, tuple[Diagnosis, Severity]] = {}

    async def ingest(self, reading: SensorReading) -> None:
        """Ingest one telemetry reading and publish any derived feed messages."""
        if reading.kind is SignalKind.POWER and reading.power is not None:
            self._power = reading.power
            return
        if reading.kind is SignalKind.GENERATOR and reading.generator is not None:
            self._generator = reading.generator
            return
        if reading.kind is SignalKind.ENERGY and reading.energy_w is not None:
            self._energy[reading.asset_id] = reading.energy_w
            return
        if reading.kind is not SignalKind.TEMPERATURE or reading.temp_c is None:
            return
        self._window[reading.asset_id].append(reading)
        await self._emit(reading)

    async def _emit(self, latest: SensorReading) -> None:
        """Publish the fridge tile state and any changed alert state for an asset."""
        asset_id = latest.asset_id
        profile = PROFILES[self._profiles.get(asset_id, "chilled")]
        readings = list(self._window[asset_id])
        prediction = predict_time_to_unsafe(readings, profile=profile)
        slope = prediction.slope_c_per_min if prediction else 0.0
        diagnosis = diagnose(
            Snapshot(
                self._power,
                self._generator,
                slope,
                self._energy.get(asset_id, 180.0),
                self._door_min.get(asset_id, 0.0),
            )
        )
        probability = spoilage_probability(prediction, restore_eta_min=self._eta)
        at_risk = stock_at_risk(
            self._values_c,
            {asset_id} if probability > 0 else set(),
            probability,
        )
        temp = float(latest.temp_c)
        status = _status(temp, profile.safe_max_c, profile.unsafe_above_c, prediction is not None)
        await self._publish(
            "fridge",
            {
                "asset_id": asset_id,
                "profile": profile.name,
                "temp_c": round(temp, 2),
                "status": status,
                "minutes_to_unsafe": (
                    prediction.minutes_to_unsafe if prediction else None
                ),
                "stock_at_risk": at_risk.to_dict(),
                "ts": latest.ts.isoformat(),
                "trend": [
                    round(float(reading.temp_c), 2)
                    for reading in readings
                    if reading.temp_c is not None
                ],
                "synthetic": latest.synthetic,
            },
        )
        await self._publish_alert(
            latest=latest,
            diagnosis=diagnosis,
            prediction=prediction,
            at_risk=at_risk,
        )

    async def _publish_alert(self, *, latest, diagnosis, prediction, at_risk) -> None:
        """Emit a cold-chain alert only when the alert state changes."""
        asset_id = latest.asset_id
        key = (diagnosis.diagnosis, diagnosis.severity)
        if diagnosis.severity >= Severity.WARNING and self._last_alert.get(asset_id) != key:
            self._last_alert[asset_id] = key
            signals = [latest.sensor_id]
            if self._power is PowerState.OUTAGE:
                signals.extend([f"{latest.site_id}_mains", f"{latest.site_id}_gen"])
            alert = build_alert(
                site_id=latest.site_id,
                asset_id=asset_id,
                dr=diagnosis,
                pred=prediction,
                stock_at_risk=at_risk,
                signals=signals,
                ts=latest.ts,
            )
            await self._publish("cold_chain", alert.to_dict())
        elif diagnosis.severity is Severity.INFO:
            self._last_alert.pop(asset_id, None)


async def run_demo_feed(
    publish: Publish,
    *,
    interval_s: float = 2.0,
    fridges: Iterable[FridgeSpec] = DEMO_FRIDGES,
    scenario: PowerScenario = DEMO_SCENARIO,
    minutes: int = 60,
) -> None:
    """Replay the labeled cold-chain drill at demo pace until cancelled."""
    specs = list(fridges)
    while True:
        feed = ColdChainFeed(
            publish,
            values_c={fridge.asset_id: fridge.contents_value_c for fridge in specs},
            profiles={fridge.asset_id: fridge.profile for fridge in specs},
        )
        previous_tick = None
        for reading in simulate_site(
            site_id="store_12",
            fridges=specs,
            scenario=scenario,
            minutes=minutes,
        ):
            if previous_tick is not None and reading.ts != previous_tick:
                await asyncio.sleep(interval_s)
            previous_tick = reading.ts
            await feed.ingest(reading)
        await asyncio.sleep(interval_s * 5)


def _status(
    temp_c: float,
    safe_max_c: float,
    unsafe_above_c: float,
    has_prediction: bool,
) -> str:
    """Map current temperature and prediction state into the tile status."""
    if temp_c > unsafe_above_c:
        return "unsafe"
    if has_prediction or temp_c > safe_max_c:
        return "at_risk"
    return "safe"
