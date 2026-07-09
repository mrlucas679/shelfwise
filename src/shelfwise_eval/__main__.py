from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .harness import run_backend_eval


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ShelfWise backend eval harness.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the eval report as JSON instead of a compact text scorecard.",
    )
    parser.add_argument(
        "--token-ceiling",
        type=int,
        default=24_000,
        help="Maximum estimated cascade tokens allowed for the golden path.",
    )
    args = parser.parse_args(argv)

    report = run_backend_eval(token_ceiling=args.token_ceiling)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        status = "PASS" if report.passed else "FAIL"
        print(f"SHELFWISE EVAL {status}: {report.passed_count}/{report.total_count} checks passed")
        for check in report.checks:
            marker = "PASS" if check.passed else "FAIL"
            print(f"{marker} {check.name}: {check.detail}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
