from __future__ import annotations

import argparse
from pathlib import Path

from shelfwise_benchmark.fleet_scale import run_fleet_scale_shakedown, write_fleet_scale_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic ShelfWise fleet scoring")
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--rows", type=int, default=500_000)
    parser.add_argument("--locations", type=int, default=40)
    parser.add_argument("--chunk-size", type=int, default=1_000)
    parser.add_argument("--top-limit", type=int, default=200)
    parser.add_argument("--output", type=Path, default=Path("reports/fleet-scale-shakedown.json"))
    args = parser.parse_args()
    receipt = run_fleet_scale_shakedown(
        seed=args.seed,
        rows=args.rows,
        locations=args.locations,
        chunk_size=args.chunk_size,
        top_limit=args.top_limit,
    )
    destination = write_fleet_scale_receipt(receipt, args.output)
    rows_processed = receipt["score"]["rows_processed"]
    print(f"wrote {destination}: {rows_processed} rows, {receipt['elapsed_ms']}ms")
    if not receipt["requested_rows_fully_processed"]:
        print(
            f"WARNING: requested {args.rows} rows but the fleet catalog only supplied "
            f"{rows_processed} (shortfall {receipt['rows_shortfall']}). This is a catalog-size "
            "ceiling (FLEET_SKU_TARGET in shelfwise_worldgen.catalog.generate), not a scoring "
            f"failure - do not present {args.rows} as the proven scale for this run; "
            f"{rows_processed} is the real number."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
