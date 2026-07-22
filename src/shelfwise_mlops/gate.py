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
    """Block model or prompt promotion when the golden scorecard drops below floors.

    Wiring status: this is a real, independently tested gate (`tests/test_mlops.py`),
    not a stub - but no application route, worker, or training script currently calls
    it. The live skill-promotion path (`skill_registry.promote`) gates on a single
    `measured_pass_rate < manifest.minimum_pass_rate` check instead, which has no
    per-category floor. `registry.release_gate` is a second, differently-shaped gate
    (candidate-vs-baseline regression) in this same package, also unwired. Before
    relying on either as "the" release gate for a real promotion decision, confirm
    which one (if any) the call site actually needs and wire it explicitly.
    """

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
