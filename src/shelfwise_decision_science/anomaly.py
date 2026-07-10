from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from .utils import clamp, decimal, q2, safe_div


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    metric_name: str
    value: Decimal
    score: Decimal
    is_anomaly: bool
    confidence: Decimal
    method: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "metric_name": self.metric_name,
            "value": str(self.value),
            "score": str(self.score),
            "is_anomaly": self.is_anomaly,
            "confidence": str(self.confidence),
            "method": self.method,
        }


def detect_robust_anomaly(
    *,
    metric_name: str,
    current_value: Decimal,
    history: list[Decimal],
    threshold: Decimal = Decimal("3.5"),
) -> AnomalyResult:
    values = [decimal(item) for item in history]
    current = decimal(current_value)
    if len(values) < 4:
        score = Decimal("0")
        is_anomaly = False
        confidence = Decimal("0.20")
    else:
        center = decimal(median(values))
        mad = decimal(median([abs(item - center) for item in values]))
        normalised_mad = mad * Decimal("1.4826")
        score = Decimal("0") if mad == 0 else safe_div(abs(current - center), normalised_mad)
        is_anomaly = score >= decimal(threshold)
        support = clamp(safe_div(len(values), 14))
        confidence = q2(Decimal("0.45") + support * Decimal("0.45"))
    return AnomalyResult(
        metric_name=metric_name,
        value=current,
        score=q2(score),
        is_anomaly=is_anomaly,
        confidence=confidence,
        method="median_absolute_deviation_robust_z_score",
    )
