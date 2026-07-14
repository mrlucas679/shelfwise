from __future__ import annotations

import argparse
from pathlib import Path

from shelfwise_benchmark.fleet_scale import run_fleet_scale_shakedown, write_fleet_scale_receipt


def main() -> None:
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
    print(
        f"wrote {destination}: {receipt['score']['rows_processed']} rows, "
        f"{receipt['elapsed_ms']}ms"
    )


if __name__ == "__main__":
    main()
