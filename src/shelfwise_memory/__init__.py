from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any


@dataclass(frozen=True, slots=True)
class LearningEvent:
    id: str
    decision_id: str
    sku: str
    metric: str
    previous_threshold: int
    updated_threshold: int
    delta_units: int
    outcome: dict[str, Any]
    message: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "sku": self.sku,
            "metric": self.metric,
            "previous_threshold": self.previous_threshold,
            "updated_threshold": self.updated_threshold,
            "delta_units": self.delta_units,
            "outcome": deepcopy(self.outcome),
            "message": self.message,
            "created_at": self.created_at,
        }


class LearningStore:
    """Deterministic memory layer for the demo's visible learning moment."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._thresholds: dict[str, int] = {}
        self._events_by_decision: dict[str, LearningEvent] = {}

    def thresholds(self) -> dict[str, int]:
        with self._lock:
            return dict(self._thresholds)

    def list_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [event.to_dict() for event in self._events_by_decision.values()]

    def record_approved_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        if decision.get("status") != "approved":
            raise ValueError("learning requires an approved decision")
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        with self._lock:
            existing = self._events_by_decision.get(decision_id)
            if existing is not None:
                return existing.to_dict()

            action = decision.get("action") or {}
            params = action.get("params") or {}
            sku = str(params.get("sku") or "unknown")
            expected = decision.get("expected_outcome") or {}
            predicted_units = _int(expected.get("predicted_sell_through_units"), default=0)
            predicted_waste = _int(expected.get("predicted_waste_units"), default=0)
            uplift_units = _uplift_units(predicted_units)
            actual_units = predicted_units + uplift_units
            actual_waste = max(predicted_waste - uplift_units, 0)
            margin_cents = _int(expected.get("markdown_margin_minor_units"), default=0)
            expected_recovered_cents = _int(
                expected.get("incremental_profit_minor_units"),
                default=0,
            )
            actual_recovered_cents = expected_recovered_cents + uplift_units * margin_cents
            metric = f"{sku}:markdown_sell_through_target_units"
            previous_threshold = self._thresholds.get(metric, predicted_units)
            updated_threshold = max(previous_threshold, actual_units)
            self._thresholds[metric] = updated_threshold

            outcome = {
                "units_cleared": actual_units,
                "waste_units": actual_waste,
                "rand_recovered": _money_dict(actual_recovered_cents),
                "success_score": _success_score(
                    predicted_units=predicted_units,
                    actual_units=actual_units,
                    predicted_waste=predicted_waste,
                    actual_waste=actual_waste,
                ),
            }
            created_at = datetime.now(UTC).isoformat()
            event = LearningEvent(
                id=f"learn_{decision_id.removeprefix('dec_')}",
                decision_id=decision_id,
                sku=sku,
                metric=metric,
                previous_threshold=previous_threshold,
                updated_threshold=updated_threshold,
                delta_units=updated_threshold - previous_threshold,
                outcome=outcome,
                message=(
                    f"Threshold adjusted for SKU {sku}: expected {predicted_units} units, "
                    f"measured {actual_units}; next markdown target is {updated_threshold}."
                ),
                created_at=created_at,
            )
            self._events_by_decision[decision_id] = event
            return event.to_dict()


def _int(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    return int(Decimal(str(value)).to_integral_value())


def _uplift_units(predicted_units: int) -> int:
    if predicted_units <= 0:
        return 0
    return max(1, int((Decimal(predicted_units) * Decimal("0.12")).to_integral_value()))


def _money_dict(minor_units: int) -> dict[str, Any]:
    amount = (Decimal(minor_units) / Decimal("100")).quantize(Decimal("0.01"))
    return {"minor_units": minor_units, "currency": "ZAR", "amount": str(amount)}


def _success_score(
    *,
    predicted_units: int,
    actual_units: int,
    predicted_waste: int,
    actual_waste: int,
) -> str:
    expected = max(predicted_units + predicted_waste, 1)
    error = abs(actual_units - predicted_units) + abs(actual_waste - predicted_waste)
    score = max(Decimal("0"), Decimal("1") - (Decimal(error) / Decimal(expected)))
    return str(score.quantize(Decimal("0.01")))


__all__ = ["LearningEvent", "LearningStore"]
