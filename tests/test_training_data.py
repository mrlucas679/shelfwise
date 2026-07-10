from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRAINING_DIR = ROOT / "data" / "training"


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        assert isinstance(value, dict), f"{path}:{line_no} must contain a JSON object"
        rows.append(value)
    return rows


def test_sft_smoke_dataset_is_valid_chat_jsonl() -> None:
    rows = _jsonl(TRAINING_DIR / "shelfwise_sft_smoke.jsonl")

    assert len(rows) >= 32
    for row in rows:
        assert isinstance(row.get("messages"), list)
        assert row.get("output")
        assert row.get("synthetic") is True


def test_preference_smoke_dataset_is_valid_pairwise_jsonl() -> None:
    rows = _jsonl(TRAINING_DIR / "shelfwise_preference_smoke.jsonl")

    assert len(rows) >= 16
    for row in rows:
        assert isinstance(row.get("messages"), list)
        assert isinstance(row.get("chosen"), dict)
        assert isinstance(row.get("rejected"), dict)
        assert row.get("synthetic") is True
