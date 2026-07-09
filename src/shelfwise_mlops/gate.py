from __future__ import annotations

from decimal import Decimal
from typing import Any


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def release_gate(
    scorecard: dict[str, Any],
    *,
    min_pass: Decimal = Decimal("0.95"),
    per_category_floor: Decimal = Decimal("0.90"),
) -> tuple[bool, list[str]]:
    """Block model or prompt promotion when the golden scorecard drops below floors."""

    reasons: list[str] = []
    pass_rate = scorecard.get("pass_rate")
    if pass_rate is None:
        reasons.append("overall pass_rate is missing")
    elif _decimal(pass_rate) < min_pass:
        reasons.append(f"overall pass {_decimal(pass_rate):.2%} < {min_pass:.0%}")

    by_category = scorecard.get("by_category", {})
    if not isinstance(by_category, dict):
        reasons.append("by_category must be an object")
    else:
        for category, rate in sorted(by_category.items()):
            if _decimal(rate) < per_category_floor:
                reasons.append(f"{category} {_decimal(rate):.0%} < {per_category_floor:.0%}")
    return not reasons, reasons
