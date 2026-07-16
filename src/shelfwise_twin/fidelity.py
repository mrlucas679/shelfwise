from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FidelityInputs:
    """Collect normalized evidence for one exact-store twin snapshot."""

    identity_coverage: float
    topology_coverage: float
    fresh_property_ratio: float
    provenance_ratio: float
    source_agreement: float
    projection_health: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class FidelityScore:
    """Return a transparent score with the weakest dimension visible."""

    total: float
    dimensions: dict[str, float]
    weakest_dimension: str

    def to_dict(self) -> dict[str, object]:
        """Serialize the score without hiding its component dimensions."""
        return {
            "total": self.total,
            "dimensions": dict(self.dimensions),
            "weakest_dimension": self.weakest_dimension,
        }


WEIGHTS = {
    "identity": 0.15,
    "topology": 0.10,
    "freshness": 0.20,
    "provenance": 0.15,
    "agreement": 0.15,
    "projection": 0.10,
    "calibration": 0.15,
}


def calculate_fidelity(value: FidelityInputs) -> FidelityScore:
    """Calculate a weighted score while keeping every weak dimension observable."""
    dimensions = {
        "identity": _bounded(value.identity_coverage),
        "topology": _bounded(value.topology_coverage),
        "freshness": _bounded(value.fresh_property_ratio),
        "provenance": _bounded(value.provenance_ratio),
        "agreement": _bounded(value.source_agreement),
        "projection": _bounded(value.projection_health),
        "calibration": _bounded(value.calibration_score),
    }
    total = sum(dimensions[name] * weight for name, weight in WEIGHTS.items()) * 100
    weakest = min(dimensions, key=dimensions.get)
    return FidelityScore(round(total, 1), dimensions, weakest)


def _bounded(value: float) -> float:
    """Keep malformed source metrics from producing impossible scores."""
    return max(0.0, min(1.0, float(value)))
