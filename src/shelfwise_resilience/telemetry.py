from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar


class SignalKind(StrEnum):
    TEMPERATURE = "temperature"
    POWER = "power"
    GENERATOR = "generator"
    DOOR = "door"
    AMBIENT = "ambient"
    ENERGY = "energy"


class Transport(StrEnum):
    LORAWAN = "lorawan"
    BLE = "ble"
    WIFI = "wifi"
    CELLULAR = "cellular"
    GATEWAY = "gateway"
    MANUAL = "manual"


class Medium(StrEnum):
    AIR = "air"
    PRODUCT = "product"


class PowerState(StrEnum):
    MAINS = "mains"
    OUTAGE = "outage"


class GeneratorState(StrEnum):
    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    LOW_FUEL = "low_fuel"
    TRIPPED = "tripped"


class DoorState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class SensorReading:
    sensor_id: str
    asset_id: str
    site_id: str
    kind: SignalKind
    ts: datetime
    temp_c: float | None = None
    power: PowerState | None = None
    generator: GeneratorState | None = None
    door: DoorState | None = None
    energy_w: float | None = None
    fuel_pct: float | None = None
    medium: Medium = Medium.AIR
    transport: Transport = Transport.GATEWAY
    battery_pct: float | None = None
    quality: float = 1.0
    synthetic: bool = False

    _FIELD: ClassVar[dict[SignalKind, str]] = {
        SignalKind.TEMPERATURE: "temp_c",
        SignalKind.AMBIENT: "temp_c",
        SignalKind.POWER: "power",
        SignalKind.GENERATOR: "generator",
        SignalKind.DOOR: "door",
        SignalKind.ENERGY: "energy_w",
    }

    def __post_init__(self) -> None:
        _validate_id("sensor_id", self.sensor_id)
        _validate_id("asset_id", self.asset_id)
        _validate_id("site_id", self.site_id)
        object.__setattr__(self, "kind", SignalKind(self.kind))
        object.__setattr__(self, "ts", _aware_datetime(self.ts))
        object.__setattr__(self, "medium", Medium(self.medium))
        object.__setattr__(self, "transport", Transport(self.transport))
        if self.power is not None:
            object.__setattr__(self, "power", PowerState(self.power))
        if self.generator is not None:
            object.__setattr__(self, "generator", GeneratorState(self.generator))
        if self.door is not None:
            object.__setattr__(self, "door", DoorState(self.door))
        _validate_range("temp_c", self.temp_c, -100.0, 150.0)
        _validate_range("energy_w", self.energy_w, 0.0, 100_000.0)
        _validate_range("fuel_pct", self.fuel_pct, 0.0, 100.0)
        _validate_range("battery_pct", self.battery_pct, 0.0, 100.0)
        _validate_range("quality", self.quality, 0.0, 1.0)
        field = self._FIELD[self.kind]
        if getattr(self, field) is None:
            raise ValueError(f"{self.kind.value} reading missing {field}")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation for API and feed publication."""
        return {
            "sensor_id": self.sensor_id,
            "asset_id": self.asset_id,
            "site_id": self.site_id,
            "kind": self.kind.value,
            "ts": self.ts.isoformat(),
            "temp_c": self.temp_c,
            "power": self.power.value if self.power else None,
            "generator": self.generator.value if self.generator else None,
            "door": self.door.value if self.door else None,
            "energy_w": self.energy_w,
            "fuel_pct": self.fuel_pct,
            "medium": self.medium.value,
            "transport": self.transport.value,
            "battery_pct": self.battery_pct,
            "quality": self.quality,
            "synthetic": self.synthetic,
        }


def _validate_id(name: str, value: str) -> None:
    """Validate bounded device identifiers before telemetry enters the predictor."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError(f"{name} must be 1-64 characters")


def _validate_range(name: str, value: float | None, low: float, high: float) -> None:
    """Reject physically implausible sensor values at the trust boundary."""
    if value is None:
        return
    if not low <= float(value) <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def _aware_datetime(value: datetime | str) -> datetime:
    """Parse timestamps while preserving any supplied offset."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("ts must be a datetime or ISO datetime string")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
