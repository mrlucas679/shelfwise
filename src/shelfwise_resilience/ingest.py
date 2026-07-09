from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from .telemetry import SensorReading, SignalKind, Transport


class TelemetrySource(Protocol):
    async def read(self) -> list[SensorReading]:
        """Return normalized readings from a gateway, poller, or replay source."""


MONNIT_MAP = {
    "sensor_id": "sensorID",
    "temp_c": "temperatureC",
    "battery_pct": "battery",
    "ts": "timestamp",
}
BLE_GATEWAY_MAP = {
    "sensor_id": "mac",
    "temp_c": "t",
    "battery_pct": "bat",
    "ts": "time",
}


def normalize(
    raw: dict,
    *,
    mapping: dict[str, str],
    kind: SignalKind,
    transport: Transport,
) -> SensorReading:
    """Map a vendor payload into the standard telemetry contract."""

    def get(field: str, default: object = None) -> object:
        return raw.get(mapping.get(field, field), default)

    battery = get("battery_pct")
    return SensorReading(
        sensor_id=str(get("sensor_id")),
        asset_id=str(get("asset_id")),
        site_id=str(get("site_id")),
        kind=kind,
        ts=_parse_ts(get("ts")),
        temp_c=_optional_float(get("temp_c")),
        power=get("power"),
        generator=get("generator"),
        door=get("door"),
        energy_w=_optional_float(get("energy_w")),
        fuel_pct=_optional_float(get("fuel_pct")),
        battery_pct=_optional_float(battery),
        transport=transport,
        quality=0.5 if battery is not None and float(battery) < 15 else 1.0,
        synthetic=bool(get("synthetic", False)),
    )


def _parse_ts(value: object) -> datetime:
    """Parse vendor timestamps, assuming UTC only when a timestamp is naive."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_float(value: object) -> float | None:
    """Convert optional numeric vendor fields without turning missing data into zero."""
    return None if value is None else float(value)
