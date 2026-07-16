"""Deterministic 500k-fleet scoring harness with a machine-readable receipt."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from shelfwise_worldgen.fleet import iter_fleet_batch_states, score_fleet_expiry


def run_fleet_scale_shakedown(
    *,
    seed: int,
    rows: int = 500_000,
    locations: int = 40,
    chunk_size: int = 1_000,
    top_limit: int = 200,
) -> dict[str, Any]:
    """Score a bounded fleet stream and return a complete reproducibility receipt."""
    if rows <= 0:
        raise ValueError("rows must be positive")
    started = time.perf_counter()
    summary = score_fleet_expiry(
        islice(iter_fleet_batch_states(seed, locations=locations), rows),
        chunk_size=chunk_size,
        top_limit=top_limit,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1_000)
    result = summary.to_dict()
    # `iter_fleet_batch_states` yields exactly one row per SKU in the "fleet" scale catalog,
    # which is capped at FLEET_SKU_TARGET (currently 500,000) - `islice(..., rows)` silently
    # stops early when `rows` exceeds that catalog size, with no error and no indication in
    # the CLI's own summary line that the requested scale was never reached. Surface the
    # shortfall as an explicit field so a caller can never mistake "requested 2,000,000" for
    # "achieved 2,000,000" just because the run finished without raising.
    fully_processed = summary.rows_processed >= rows
    return {
        "schema_version": "fleet-scale-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "requested_rows": rows,
        "locations": locations,
        "chunk_size": chunk_size,
        "top_limit": top_limit,
        "elapsed_ms": elapsed_ms,
        "rows_per_second": round(summary.rows_processed / max(elapsed_ms / 1_000, 0.001), 2),
        "llm_calls": 0,
        "queue_reduction_ratio": round(
            len(summary.top_candidates) / max(summary.rows_processed, 1), 6
        ),
        "requested_rows_fully_processed": fully_processed,
        "rows_shortfall": max(0, rows - summary.rows_processed),
        "score": result,
    }


def write_fleet_scale_receipt(receipt: dict[str, Any], path: str | Path) -> Path:
    """Write a stable JSON receipt and create only its requested parent directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination
