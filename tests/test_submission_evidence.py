from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "reports" / "soak_15min_20260711T042648Z" / "summary.json"


def test_compact_soak_receipt_preserves_live_required_invariants() -> None:
    receipt = json.loads(SUMMARY.read_text(encoding="utf-8"))
    totals = receipt["totals"]

    assert receipt["passed"] is True
    assert receipt["environment"]["execution"] == "live_required"
    assert totals["chat_calls"] == totals["chat_model_answers"] == 333
    assert totals["chat_offline_answers"] == 0
    assert totals["chat_errors"] == 0
    assert totals["decisions_total"] == totals["unique_decision_ids"]
    assert totals["hitl_mismatches"] == 0
    assert totals["learning_movements"] == totals["learning_movements_expected"]
    assert receipt["limitations"]


def test_compact_soak_receipt_records_raw_artifact_hashes() -> None:
    receipt = json.loads(SUMMARY.read_text(encoding="utf-8"))

    for artifact in receipt["source_artifacts"]:
        assert len(artifact["sha256"]) == 64
        assert artifact["tracked"] is False
