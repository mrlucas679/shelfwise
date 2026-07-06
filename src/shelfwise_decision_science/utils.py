from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q2(value: object) -> Decimal:
    return decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clamp(value: object, low: object = 0, high: object = 1) -> Decimal:
    v = decimal(value)
    return max(decimal(low), min(decimal(high), v))


def safe_div(numerator: object, denominator: object, default: object = 0) -> Decimal:
    den = decimal(denominator)
    return decimal(default) if den == 0 else decimal(numerator) / den
