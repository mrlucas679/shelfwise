from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .forecasting import DemandForecast
from .utils import clamp, decimal, q2

DIVERGENCE_BAND = Decimal("0.35")
FORECAST_METHOD = "tsfm_quantile_forecast_guardrailed_by_transparent_baseline"


class TsfmClient(Protocol):
    """Forecasting transport that can be backed by HTTP, tests, or a local model."""

    async def forecast(
        self,
        *,
        history: list[float],
        horizon: int,
        covariates: dict[str, list[float]] | None = None,
        quantiles: tuple[float, ...] = (0.1, 0.5, 0.9),
    ) -> Mapping[str, Sequence[object]]:
        """Return per-day quantile paths keyed by quantile name."""


class HttpTsfmClient:
    """Tiny lazy HTTP client for a served Chronos/TimesFM-style forecasting wrapper."""

    def __init__(self, *, base_url: str, model: str = "chronos2", timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    async def forecast(
        self,
        *,
        history: list[float],
        horizon: int,
        covariates: dict[str, list[float]] | None = None,
        quantiles: tuple[float, ...] = (0.1, 0.5, 0.9),
    ) -> Mapping[str, Sequence[object]]:
        """Call the external model and return its quantile response."""

        import httpx

        payload = {
            "model": self._model,
            "history": history,
            "horizon": horizon,
            "covariates": covariates or {},
            "quantiles": list(quantiles),
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(f"{self._base_url}/forecast", json=payload)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, Mapping):
            raise ValueError("TSFM response must be a JSON object")
        return data


@dataclass(frozen=True, slots=True)
class GuardrailedForecast:
    sku: str
    baseline_daily_units: Decimal
    tsfm_daily_units: Decimal
    chosen_daily_units: Decimal
    horizon_days: int
    chosen_horizon_units: Decimal
    divergence: Decimal
    within_band: bool
    requires_human_review: bool
    p10_daily_units: Decimal
    p90_daily_units: Decimal
    method: str
    confidence: Decimal
    evidence: dict[str, Any]

    @property
    def within_guardrail(self) -> bool:
        """Compatibility alias for blueprint language."""

        return self.within_band


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("quantile path must be non-empty")
    return q2(sum(values, Decimal("0")) / Decimal(len(values)))


def _quantile_path(
    response: Mapping[str, Sequence[object]],
    keys: tuple[str, ...],
    *,
    fallback: Sequence[Decimal],
    horizon: int,
) -> list[Decimal]:
    raw: Sequence[object] | None = None
    for key in keys:
        if key in response:
            raw = response[key]
            break
    if raw is None:
        return list(fallback)
    if len(raw) != horizon:
        raise ValueError("TSFM quantile path length must match the forecast horizon")

    parsed = [decimal(value) for value in raw]
    if any(not value.is_finite() for value in parsed):
        raise ValueError("TSFM quantile path contains a non-finite value")
    return parsed


async def forecast_demand_tsfm(
    client: TsfmClient,
    *,
    baseline: DemandForecast,
    history_units: list[Decimal],
    covariates: dict[str, list[float]] | None = None,
    divergence_band: Decimal = DIVERGENCE_BAND,
) -> GuardrailedForecast:
    """Run a TSFM in shadow mode and let it drive only when it stays near the baseline."""

    if not history_units:
        raise ValueError("history_units must be non-empty")
    if baseline.horizon_days <= 0:
        raise ValueError("baseline horizon_days must be positive")
    if divergence_band < 0:
        raise ValueError("divergence_band must be non-negative")

    response = await client.forecast(
        history=[float(value) for value in history_units],
        horizon=baseline.horizon_days,
        covariates=covariates,
    )
    baseline_path = [baseline.daily_units] * baseline.horizon_days
    p50 = _quantile_path(
        response,
        ("0.5", "p50", "50"),
        fallback=baseline_path,
        horizon=baseline.horizon_days,
    )
    p10 = _quantile_path(
        response,
        ("0.1", "p10", "10"),
        fallback=p50,
        horizon=baseline.horizon_days,
    )
    p90 = _quantile_path(
        response,
        ("0.9", "p90", "90"),
        fallback=p50,
        horizon=baseline.horizon_days,
    )

    base_daily = baseline.daily_units
    tsfm_daily = _mean(p50)
    denominator = base_daily if base_daily > 0 else Decimal("1")
    divergence = q2(abs(tsfm_daily - base_daily) / denominator)
    within_band = divergence <= divergence_band
    chosen_daily = tsfm_daily if within_band else base_daily
    confidence = clamp(
        baseline.confidence if within_band else baseline.confidence * Decimal("0.60")
    )

    evidence: dict[str, Any] = {
        "method": FORECAST_METHOD,
        "baseline_method": baseline.method,
        "baseline_daily_units": str(base_daily),
        "tsfm_daily_units": str(tsfm_daily),
        "chosen_daily_units": str(chosen_daily),
        "divergence": str(divergence),
        "band": str(q2(divergence_band)),
        "within_band": within_band,
        "requires_human_review": not within_band,
        "horizon_days": baseline.horizon_days,
        "history_points": len(history_units),
        "covariate_keys": sorted((covariates or {}).keys()),
        "p10_daily_units": str(_mean(p10)),
        "p50_path": [str(q2(value)) for value in p50],
        "p90_daily_units": str(_mean(p90)),
        "decision": (
            "TSFM p50 stayed inside the transparent-baseline guardrail"
            if within_band
            else "TSFM p50 diverged; baseline kept control and review was raised"
        ),
    }

    return GuardrailedForecast(
        sku=baseline.sku,
        baseline_daily_units=base_daily,
        tsfm_daily_units=tsfm_daily,
        chosen_daily_units=chosen_daily,
        horizon_days=baseline.horizon_days,
        chosen_horizon_units=q2(chosen_daily * baseline.horizon_days),
        divergence=divergence,
        within_band=within_band,
        requires_human_review=not within_band,
        p10_daily_units=_mean(p10),
        p90_daily_units=_mean(p90),
        method=FORECAST_METHOD,
        confidence=q2(confidence),
        evidence=evidence,
    )


def wape(actuals: list[Decimal], forecasts: list[Decimal]) -> Decimal:
    """Calculate weighted absolute percentage error for TSFM shadow promotion gates."""

    if not actuals or len(actuals) != len(forecasts):
        raise ValueError("actuals and forecasts must be equal-length and non-empty")
    denominator = sum((abs(actual) for actual in actuals), Decimal("0"))
    if denominator == 0:
        return Decimal("0.00")
    numerator = sum(
        (abs(actual - forecast) for actual, forecast in zip(actuals, forecasts, strict=True)),
        Decimal("0"),
    )
    return q2(numerator / denominator)
