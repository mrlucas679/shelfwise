"""Cold-chain thermal physics: warming-trend prediction from telemetry.

Fits a short linear trend through recent temperature readings for an asset
and projects it forward to estimate when the asset will cross its profile's
"unsafe" threshold. This lets the feed/alerting layer warn before stock
spoils rather than after, without needing a full physical simulation model.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .telemetry import SensorReading

_MIN_READINGS = 2


@dataclass(frozen=True, slots=True)
class ColdChainProfile:
    """Temperature safety thresholds (Celsius) for a class of cold storage."""

    name: str
    safe_max_c: float
    unsafe_above_c: float


@dataclass(frozen=True, slots=True)
class Prediction:
    """A short-horizon warming forecast derived from recent readings."""

    slope_c_per_min: float
    minutes_to_unsafe: float


PROFILES: dict[str, ColdChainProfile] = {
    # South African cold-chain practice: dairy/produce held at ~2-5C, with
    # 8C treated as the regulatory/food-safety excursion threshold.
    "chilled": ColdChainProfile(name="chilled", safe_max_c=5.0, unsafe_above_c=8.0),
    # Frozen goods (meat, ice cream): ideal storage around -18C to -20C,
    # with -12C treated as the quality/safety abuse threshold.
    "frozen": ColdChainProfile(name="frozen", safe_max_c=-15.0, unsafe_above_c=-12.0),
}

# Default single-profile reference for call sites that need one physics
# constant rather than a name-keyed lookup (most cold-chain assets in a
# retail store are chilled dairy/produce, not frozen).
PHYSICS_PROFILE = PROFILES["chilled"]


def predict_time_to_unsafe(
    readings: Sequence[SensorReading], *, profile: ColdChainProfile
) -> Prediction | None:
    """Project a linear warming trend forward to the profile's unsafe threshold.

    Returns None when there isn't enough data to fit a trend, or the fitted
    trend isn't actually warming (flat or cooling) -- callers treat None as
    "no imminent thermal risk from the current trend".
    """
    ordered = sorted(readings, key=lambda reading: reading.ts)
    points = [
        (reading.ts.timestamp() / 60.0, reading.temp_c)
        for reading in ordered
        if reading.temp_c is not None
    ]
    if len(points) < _MIN_READINGS or len({time for time, _temp in points}) < _MIN_READINGS:
        return None

    times = [time for time, _temp in points]
    temps = [temp for _time, temp in points]
    slope, _intercept = statistics.linear_regression(times, temps)
    if slope <= 0:
        return None

    # Project from the latest observed temperature (ground truth) using the
    # regression-fitted slope (robust to single-reading noise).
    current_temp = temps[-1]
    minutes_to_unsafe = (profile.unsafe_above_c - current_temp) / slope
    return Prediction(slope_c_per_min=slope, minutes_to_unsafe=max(0.0, minutes_to_unsafe))
