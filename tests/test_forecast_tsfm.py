from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from shelfwise_decision_science import forecast_demand
from shelfwise_decision_science.forecast_tsfm import forecast_demand_tsfm, wape


class FakeTsfm:
    def __init__(self, p50: Decimal) -> None:
        self._p50 = p50
        self.seen: dict[str, Any] = {}

    async def forecast(
        self,
        *,
        history: list[float],
        horizon: int,
        covariates: dict[str, list[float]] | None = None,
        quantiles: tuple[float, ...] = (0.1, 0.5, 0.9),
    ) -> dict[str, list[str]]:
        self.seen = {
            "history": history,
            "horizon": horizon,
            "covariates": covariates,
            "quantiles": quantiles,
        }
        return {
            "0.1": [str(self._p50 * Decimal("0.80"))] * horizon,
            "0.5": [str(self._p50)] * horizon,
            "0.9": [str(self._p50 * Decimal("1.20"))] * horizon,
        }


def _baseline():
    return forecast_demand(
        sku="4011",
        recent_daily_units=[Decimal("20")] * 14,
        horizon_days=3,
        payday_multiplier=Decimal("1"),
    )


def test_agreeing_tsfm_is_chosen_and_cites_both_numbers() -> None:
    base = _baseline()
    fake = FakeTsfm(base.daily_units * Decimal("1.10"))

    forecast = asyncio.run(
        forecast_demand_tsfm(
            fake,
            baseline=base,
            history_units=[Decimal("20")] * 14,
            covariates={"payday": [0.0, 1.0]},
        )
    )

    assert forecast.within_band is True
    assert forecast.within_guardrail is True
    assert forecast.requires_human_review is False
    assert forecast.chosen_daily_units == forecast.tsfm_daily_units
    assert forecast.chosen_horizon_units == Decimal("66.00")
    assert forecast.evidence["baseline_daily_units"] == str(base.daily_units)
    assert forecast.evidence["tsfm_daily_units"] == str(forecast.tsfm_daily_units)
    assert fake.seen["covariates"] == {"payday": [0.0, 1.0]}


def test_diverging_tsfm_falls_back_to_baseline_and_raises_review() -> None:
    base = _baseline()
    fake = FakeTsfm(base.daily_units * Decimal("3"))

    forecast = asyncio.run(
        forecast_demand_tsfm(
            fake,
            baseline=base,
            history_units=[Decimal("20")] * 14,
        )
    )

    assert forecast.within_band is False
    assert forecast.requires_human_review is True
    assert forecast.chosen_daily_units == base.daily_units
    assert forecast.divergence > Decimal("0.35")
    assert forecast.confidence < base.confidence
    assert forecast.evidence["decision"].startswith("TSFM p50 diverged")


def test_wape_is_the_promotion_gate_metric() -> None:
    actuals = [Decimal("10"), Decimal("20"), Decimal("30")]

    assert wape(actuals, actuals) == Decimal("0.00")
    assert wape(actuals, [Decimal("12"), Decimal("22"), Decimal("28")]) == Decimal("0.10")


class _TimingOutTsfm:
    async def forecast(self, **kwargs):
        raise TimeoutError("simulated transport timeout")


class _ConnectionRefusedTsfm:
    async def forecast(self, **kwargs):
        raise OSError("connection refused")


class _MalformedTsfm:
    def __init__(self, response):
        self._response = response

    async def forecast(self, **kwargs):
        return self._response


@pytest.mark.parametrize(
    "client",
    [
        _TimingOutTsfm(),
        _ConnectionRefusedTsfm(),
        _MalformedTsfm({"0.5": ["nan", "nan", "nan"]}),
        _MalformedTsfm({"0.5": [1.0]}),  # wrong horizon length
    ],
    ids=["timeout", "connection-refused", "non-finite-values", "wrong-path-length"],
)
def test_tsfm_transport_failure_degrades_to_baseline_not_a_crash(client) -> None:
    """Shadow mode means the baseline is authoritative: a TSFM that times out, refuses
    connections, or answers with an unusable payload must degrade to the transparent
    baseline with the failure on the evidence record - never crash the caller and never
    masquerade as agreement (found 2026-07-15: no failure-handling path existed at all).
    """
    baseline = _baseline()
    result = asyncio.run(
        forecast_demand_tsfm(
            client,
            baseline=baseline,
            history_units=[Decimal("10")] * 8,
        )
    )

    assert result.chosen_daily_units == baseline.daily_units
    assert result.within_band is False
    assert result.requires_human_review is False, (
        "an absent shadow forecast must not degrade operations - the baseline is "
        "exactly as trustworthy as it was without the TSFM"
    )
    assert result.evidence["tsfm_error"], "the failure must be on the record"
    assert "baseline kept control" in result.evidence["decision"]


def test_tsfm_input_validation_errors_still_raise() -> None:
    """Caller bugs (empty history) are not model-serving weather; they must still raise."""
    with pytest.raises(ValueError, match="history_units"):
        asyncio.run(
            forecast_demand_tsfm(
                _TimingOutTsfm(),
                baseline=_baseline(),
                history_units=[],
            )
        )
