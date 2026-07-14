from __future__ import annotations

import json

from shelfwise_benchmark.fleet_scale import run_fleet_scale_shakedown, write_fleet_scale_receipt


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
