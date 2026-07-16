from __future__ import annotations

import json
from decimal import Decimal

import shelfwise_benchmark.fleet_scale as fleet_scale_module
from shelfwise_benchmark.fleet_scale import run_fleet_scale_shakedown, write_fleet_scale_receipt
from shelfwise_contracts import Money
from shelfwise_worldgen.fleet import FleetBatchState


def test_fleet_scale_receipt_is_model_free_and_bounded(tmp_path) -> None:
    receipt = run_fleet_scale_shakedown(seed=7, rows=2_000, locations=4, top_limit=10)

    assert receipt["score"]["rows_processed"] == 2_000
    assert receipt["score"]["chunks_processed"] == 2
    assert receipt["llm_calls"] == 0
    assert len(receipt["score"]["top_candidates"]) <= 10
    assert receipt["queue_reduction_ratio"] < 0.01

    path = write_fleet_scale_receipt(receipt, tmp_path / "receipt.json")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "fleet-scale-v1"
    assert receipt["requested_rows_fully_processed"] is True
    assert receipt["rows_shortfall"] == 0


def test_requesting_more_rows_than_the_source_holds_is_flagged_not_silent(monkeypatch) -> None:
    """The real fleet catalog (FLEET_SKU_TARGET, currently 500,000) caps how many rows
    `iter_fleet_batch_states` can ever yield - `islice(..., rows)` used to stop early with no
    signal that a requested scale (e.g. 2,000,000 rows) was never actually reached. Exercised
    here against a small fake generator (not the real 500k catalog) purely for test speed; the
    shortfall-detection logic being tested is backend-agnostic."""

    def _small_source(seed: int, *, locations: int = 40):
        for index in range(10):
            yield FleetBatchState(
                sku=f"SKU-{index}",
                location_id=f"store_{index % locations + 1:03d}",
                lot_id=f"LOT-SKU-{index}-01",
                units_on_hand=10,
                days_to_expiry=2,
                forecast_daily_units=Decimal(1),
                unit_cost=Money.zar(Decimal("10.00")),
                cold_chain_risk=Decimal("0.1"),
            )

    monkeypatch.setattr(fleet_scale_module, "iter_fleet_batch_states", _small_source)

    receipt = run_fleet_scale_shakedown(seed=7, rows=600_000, locations=4, top_limit=10)

    assert receipt["requested_rows"] == 600_000
    assert receipt["score"]["rows_processed"] == 10
    assert receipt["requested_rows_fully_processed"] is False
    assert receipt["rows_shortfall"] == 600_000 - 10
