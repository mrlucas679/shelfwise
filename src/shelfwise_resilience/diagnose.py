from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from .telemetry import GeneratorState, PowerState

_RISE_C_PER_MIN = 0.05
_IDLE_W = 20.0
_DOOR_AJAR_MIN = 10.0


class Diagnosis(StrEnum):
    NORMAL = "normal"
    ON_GENERATOR = "on_generator"
    GENERATOR_FAILED = "generator_failed"
    UNIT_NOT_COOLING = "unit_not_cooling"
    COMPRESSOR_FAILURE = "compressor_failure"
    DOOR_AJAR = "door_ajar"
    WARMING = "warming"


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    CRITICAL = 2


@dataclass(frozen=True, slots=True)
class Snapshot:
    power: PowerState
    generator: GeneratorState
    temp_slope_c_per_min: float
    energy_w: float
    door_open_min: float = 0.0


@dataclass(frozen=True, slots=True)
class DiagnosisResult:
    diagnosis: Diagnosis
    severity: Severity
    headline: str
    recommended_action: str

    def to_dict(self) -> dict[str, int | str]:
        """Return a JSON-ready diagnosis for alerts and traces."""
        return {
            "diagnosis": self.diagnosis.value,
            "severity": int(self.severity),
            "headline": self.headline,
            "recommended_action": self.recommended_action,
        }


def diagnose(snapshot: Snapshot) -> DiagnosisResult:
    """Fuse power, generator, temperature trend, energy, and door state."""
    power = PowerState(snapshot.power)
    generator = GeneratorState(snapshot.generator)
    rising = snapshot.temp_slope_c_per_min >= _RISE_C_PER_MIN
    cooling_expected = power is PowerState.MAINS or generator is GeneratorState.RUNNING

    if snapshot.door_open_min >= _DOOR_AJAR_MIN:
        return DiagnosisResult(
            Diagnosis.DOOR_AJAR,
            Severity.WARNING,
            "Door left open; temperature drifting.",
            "Close door and check seal or latch.",
        )
    if power is PowerState.OUTAGE and generator in {
        GeneratorState.FAILED,
        GeneratorState.OFF,
        GeneratorState.TRIPPED,
        GeneratorState.STARTING,
    }:
        return DiagnosisResult(
            Diagnosis.GENERATOR_FAILED,
            Severity.CRITICAL,
            "Power outage and generator not carrying load; cold rooms at risk.",
            "Dispatch technician now; move high-value stock to backup refrigeration.",
        )
    if cooling_expected and rising and snapshot.energy_w <= _IDLE_W:
        return DiagnosisResult(
            Diagnosis.COMPRESSOR_FAILURE,
            Severity.CRITICAL,
            "Powered but compressor draws no energy while warming.",
            "Dispatch refrigeration technician; relocate stock pre-emptively.",
        )
    if cooling_expected and rising:
        return DiagnosisResult(
            Diagnosis.UNIT_NOT_COOLING,
            Severity.WARNING,
            "Cooling is on but the unit is not holding temperature.",
            "Inspect the unit, load, and door state.",
        )
    if generator is GeneratorState.LOW_FUEL:
        return DiagnosisResult(
            Diagnosis.ON_GENERATOR,
            Severity.WARNING,
            "On generator with low fuel.",
            "Refuel generator before runtime expires.",
        )
    if rising:
        return DiagnosisResult(
            Diagnosis.WARMING,
            Severity.WARNING,
            "Temperature rising.",
            "Investigate the cause.",
        )
    if power is PowerState.OUTAGE and generator is GeneratorState.RUNNING:
        return DiagnosisResult(
            Diagnosis.ON_GENERATOR,
            Severity.INFO,
            "Mains down; generator is holding temperatures stable.",
            "Monitor fuel and runtime.",
        )
    return DiagnosisResult(
        Diagnosis.NORMAL,
        Severity.INFO,
        "Cold chain nominal.",
        "None.",
    )
