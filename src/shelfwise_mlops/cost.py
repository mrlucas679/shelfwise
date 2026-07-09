from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Money


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class CostEstimate:
    provider: str
    model: str
    usage: TokenUsage
    zar: Money
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "zar": self.zar.to_dict(),
            "method": self.method,
        }


def estimate_cost(
    *,
    provider: str,
    model: str,
    usage: TokenUsage,
    input_zar_per_1k: Decimal = Decimal("0.01"),
    output_zar_per_1k: Decimal = Decimal("0.03"),
) -> CostEstimate:
    input_cost = Decimal(usage.input_tokens) / Decimal("1000") * input_zar_per_1k
    output_cost = Decimal(usage.output_tokens) / Decimal("1000") * output_zar_per_1k
    return CostEstimate(
        provider=provider,
        model=model,
        usage=usage,
        zar=Money.zar(input_cost + output_cost),
        method="token_usage_x_provider_rate_card",
    )


def inference_cost(total_tokens: int, *, rate_zar_per_1k: Decimal) -> Money:
    """Convert one call or cascade token total into a ZAR cost line."""

    if total_tokens < 0:
        raise ValueError("tokens must be non-negative")
    return Money.zar(Decimal(total_tokens) / Decimal("1000") * rate_zar_per_1k)


def decision_economics(
    *,
    rand_recovered: Money | None,
    total_tokens: int,
    rate_zar_per_1k: Decimal,
) -> dict[str, Any]:
    """Attach cost and recovered-per-cost attribution to a business decision."""

    cost = inference_cost(total_tokens, rate_zar_per_1k=rate_zar_per_1k)
    recovered = rand_recovered or Money.zar(0)
    ratio = (
        Decimal(recovered.minor_units) / Decimal(cost.minor_units)
        if cost.minor_units
        else None
    )
    return {
        "cost": cost.to_dict(),
        "recovered": recovered.to_dict(),
        "recovered_per_cost": str(ratio.quantize(Decimal("0.1"))) if ratio else None,
        "total_tokens": total_tokens,
    }
