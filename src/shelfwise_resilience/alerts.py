from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shelfwise_contracts import Money

from .diagnose import Diagnosis, DiagnosisResult, Severity
from .thermal import Prediction


@dataclass(frozen=True, slots=True)
class ColdChainAlert:
    id: str
    site_id: str
    asset_id: str
    severity: Severity
    diagnosis: Diagnosis
    headline: str
    recommended_action: str
    predicted_minutes_to_unsafe: float | None
    stock_at_risk: Money
    signals: list[str]
    ts: datetime
    synthetic: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return the frontend/feed alert shape."""
        return {
            "id": self.id,
            "site_id": self.site_id,
            "asset_id": self.asset_id,
            "severity": int(self.severity),
            "diagnosis": self.diagnosis.value,
            "headline": self.headline,
            "recommended_action": self.recommended_action,
            "predicted_minutes_to_unsafe": self.predicted_minutes_to_unsafe,
            "stock_at_risk": self.stock_at_risk.to_dict(),
            "signals": list(self.signals),
            "ts": self.ts.isoformat(),
            "synthetic": self.synthetic,
        }

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        """Mirror Pydantic's API for callers ported from the blueprint."""
        _ = mode
        return self.to_dict()


def build_alert(
    *,
    site_id: str,
    asset_id: str,
    dr: DiagnosisResult,
    pred: Prediction | None,
    stock_at_risk: Money,
    signals: list[str],
    ts: datetime,
) -> ColdChainAlert:
    """Build the business alert emitted by the cold-chain feed."""
    return ColdChainAlert(
        id=f"cca_{asset_id}_{int(ts.timestamp())}",
        site_id=site_id,
        asset_id=asset_id,
        severity=dr.severity,
        diagnosis=dr.diagnosis,
        headline=dr.headline,
        recommended_action=dr.recommended_action,
        predicted_minutes_to_unsafe=pred.minutes_to_unsafe if pred else None,
        stock_at_risk=stock_at_risk,
        signals=signals,
        ts=ts,
    )


def excursion_overlay(*, area: str, category: str, measured_outage_hours: float) -> dict[str, Any]:
    """Bridge sensor-measured excursions into the existing cascade context."""
    return {
        "area": area,
        "category": category,
        "measured_outage_hours": measured_outage_hours,
    }
