from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_full_system_artifact import validate_artifact
from shelfwise_eval.full_system import REQUIRED_FEATURE_RECEIPTS, REQUIRED_ROUTE_RECEIPTS


def test_historical_live_run_with_offline_answers_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "run"
    artifact.mkdir()
    features = [
        {"feature": name, "passed": True, "detail": "ok", "route": "", "evidence": {}}
        for name in REQUIRED_FEATURE_RECEIPTS
    ]
    routes = [
        {
            "key": name,
            "feature": "test",
            "status_code": 200,
            "ok": True,
            "request_index": index,
        }
        for index, name in enumerate(REQUIRED_ROUTE_RECEIPTS, start=1)
    ]
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "config": {"live_required": True},
                "totals": {
                    "chat_calls": 10,
                    "chat_model_answers": 2,
                    "chat_offline_answers": 8,
                    "chat_errors": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (artifact / "decision_trail.jsonl").write_text("", encoding="utf-8")
    (artifact / "feature_receipts.json").write_text(json.dumps(features), encoding="utf-8")
    (artifact / "route_receipts.json").write_text(json.dumps(routes), encoding="utf-8")

    result = validate_artifact(artifact)

    assert result["original_exit_code"] == 0
    assert result["validated_exit_code"] == 1
    assert "live_model_answer_mismatch:model=2:calls=10" in result["failures"]
    assert "live_offline_answers:8" in result["failures"]
