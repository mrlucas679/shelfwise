from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _field(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    raise ValueError(f"record missing one of: {', '.join(names)}")


def export_sft_jsonl(records: Iterable[Mapping[str, Any]], path: str | Path) -> int:
    """Write synthetic SFT records as JSONL for a dormant adapter-training pipeline."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            line = {
                "messages": _field(record, "input", "messages"),
                "output": _field(record, "ideal", "output"),
                "synthetic": True,
            }
            handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count


def export_preference_jsonl(records: Iterable[Mapping[str, Any]], path: str | Path) -> int:
    """Write synthetic preference pairs for a future DPO/ORPO pass after SFT exists."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            line = {
                "messages": _field(record, "input", "messages"),
                "chosen": _field(record, "chosen", "accepted"),
                "rejected": _field(record, "rejected", "declined"),
                "synthetic": True,
            }
            handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count
