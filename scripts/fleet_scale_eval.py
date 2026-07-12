"""Run the deterministic 500k-SKU fleet expiry scoring proof and save its receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shelfwise_worldgen.fleet import iter_fleet_batch_states, score_fleet_expiry


def main() -> int:
    """Execute the fleet scorer without loading the generated catalog into memory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20_260_712)
    parser.add_argument("--locations", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = score_fleet_expiry(iter_fleet_batch_states(args.seed, locations=args.locations))
    receipt = summary.to_dict()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "top_candidates"},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
