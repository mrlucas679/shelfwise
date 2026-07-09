from __future__ import annotations

from .harness import format_report, run_eval


def main() -> int:
    result = run_eval()
    print(format_report(result))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
