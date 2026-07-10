from __future__ import annotations

from shelfwise_contracts import Money

from .thermal import Prediction


def spoilage_probability(prediction: Prediction | None, *, restore_eta_min: float) -> float:
    """Estimate spoilage risk from crossing time versus restoration ETA."""
    if prediction is None:
        return 0.0
    if prediction.minutes_to_unsafe <= 0:
        return 1.0
    ratio = restore_eta_min / (prediction.minutes_to_unsafe + restore_eta_min)
    return max(0.0, min(1.0, ratio))


def stock_at_risk(asset_values_c: dict[str, int], at_risk: set[str], probability: float) -> Money:
    """Convert asset exposure and spoilage probability into integer ZAR cents."""
    exposure_cents = sum(value for asset, value in asset_values_c.items() if asset in at_risk)
    bounded_probability = max(0.0, min(1.0, probability))
    return Money(minor_units=round(exposure_cents * bounded_probability), currency="ZAR")
