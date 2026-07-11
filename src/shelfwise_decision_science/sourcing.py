from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .utils import decimal, q2

_SOURCE_KINDS: dict[str, str] = {
    "branch": "branch",
    "distribution_center": "regional distribution centre",
    "supplier": "supplier",
}

_ACTION_FOR_KIND: dict[str, str] = {
    "branch": "transfer_from_branch",
    "distribution_center": "transfer_from_distribution_center",
    "supplier": "expedite_supplier_order",
}


@dataclass(frozen=True, slots=True)
class StockSourceCandidate:
    """One place that might be able to cover a stock shortage."""

    source_type: str
    source_id: str
    available_units: int
    distance_km: Decimal
    lead_time_hours: Decimal
    unit_cost: Decimal | None = None

    def __post_init__(self) -> None:
        if self.source_type not in _SOURCE_KINDS:
            raise ValueError(f"unknown source_type: {self.source_type}")
        if self.available_units < 0:
            raise ValueError("available_units cannot be negative")
        if decimal(self.distance_km) < 0:
            raise ValueError("distance_km cannot be negative")
        if decimal(self.lead_time_hours) <= 0:
            raise ValueError("lead_time_hours must be positive")


@dataclass(frozen=True, slots=True)
class RankedStockSource:
    source_type: str
    source_id: str
    available_units: int
    distance_km: Decimal
    lead_time_hours: Decimal
    unit_cost: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "available_units": self.available_units,
            "distance_km": str(q2(self.distance_km)),
            "lead_time_hours": str(q2(self.lead_time_hours)),
            "unit_cost": str(q2(self.unit_cost)) if self.unit_cost is not None else None,
        }


@dataclass(frozen=True, slots=True)
class StockSourcingPlan:
    """Which real source should cover a shortage, and why, in that order."""

    sku: str
    units_needed: int
    candidates_considered: int
    eligible_considered: int
    selected_source_type: str | None
    selected_source_id: str | None
    units_sourced: int
    remaining_gap_units: int
    recommended_action: str
    conclusion: str
    ranked: tuple[RankedStockSource, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "units_needed": self.units_needed,
            "candidates_considered": self.candidates_considered,
            "eligible_considered": self.eligible_considered,
            "selected_source_type": self.selected_source_type,
            "selected_source_id": self.selected_source_id,
            "units_sourced": self.units_sourced,
            "remaining_gap_units": self.remaining_gap_units,
            "recommended_action": self.recommended_action,
            "conclusion": self.conclusion,
            "ranked": [item.to_dict() for item in self.ranked],
        }


def plan_stock_sourcing(
    *,
    sku: str,
    units_needed: int,
    candidates: tuple[StockSourceCandidate, ...],
) -> StockSourcingPlan:
    """Rank real candidate sources for a shortage and explain the chosen one.

    Never assumes stock can simply be transferred: checks nearby branches, the regional
    distribution centre, and approved suppliers, ranks whichever have any stock by lead
    time (fastest first), then distance, then cost, and falls back to recommending a
    purchase order - with an honest reason - if nothing can supply it.
    """
    if units_needed <= 0:
        raise ValueError("units_needed must be positive")

    eligible = [candidate for candidate in candidates if candidate.available_units > 0]
    ranked = sorted(
        eligible,
        key=lambda item: (
            decimal(item.lead_time_hours),
            decimal(item.distance_km),
            decimal(item.unit_cost) if item.unit_cost is not None else Decimal("0"),
            item.source_id,
        ),
    )
    ranked_sources = tuple(
        RankedStockSource(
            source_type=item.source_type,
            source_id=item.source_id,
            available_units=item.available_units,
            distance_km=decimal(item.distance_km),
            lead_time_hours=decimal(item.lead_time_hours),
            unit_cost=decimal(item.unit_cost) if item.unit_cost is not None else None,
        )
        for item in ranked
    )

    if not ranked_sources:
        return StockSourcingPlan(
            sku=sku,
            units_needed=units_needed,
            candidates_considered=len(candidates),
            eligible_considered=0,
            selected_source_type=None,
            selected_source_id=None,
            units_sourced=0,
            remaining_gap_units=units_needed,
            recommended_action="place_purchase_order",
            conclusion=(
                f"Checked {len(candidates)} possible source(s) for {sku} (branches, the "
                "regional distribution centre, and suppliers) and none has any stock "
                f"available right now - place a purchase order for the full {units_needed} "
                "units needed."
            ),
            ranked=ranked_sources,
        )

    best = ranked_sources[0]
    units_sourced = min(best.available_units, units_needed)
    remaining = units_needed - units_sourced
    kind_label = _SOURCE_KINDS[best.source_type]
    action = _ACTION_FOR_KIND[best.source_type]

    reason = (
        f"has {best.available_units} units on hand, is {q2(best.distance_km)} km away, "
        f"and can deliver in {q2(best.lead_time_hours)} hours"
    )
    runner_up = ranked_sources[1] if len(ranked_sources) > 1 else None
    comparison = ""
    if runner_up is not None:
        if runner_up.lead_time_hours != best.lead_time_hours:
            comparison = (
                f", chosen over {_SOURCE_KINDS[runner_up.source_type]} {runner_up.source_id} "
                f"({q2(runner_up.lead_time_hours)}h away) for a faster delivery"
            )
        elif runner_up.distance_km != best.distance_km:
            comparison = (
                f", chosen over {_SOURCE_KINDS[runner_up.source_type]} {runner_up.source_id} "
                "for being closer"
            )
        else:
            comparison = (
                f", chosen over {_SOURCE_KINDS[runner_up.source_type]} {runner_up.source_id} "
                "for a lower cost"
            )

    conclusion = (
        f"Source {units_sourced} of the {units_needed} units of {sku} needed from "
        f"{kind_label} {best.source_id}: it {reason}{comparison}."
    )
    if remaining > 0:
        conclusion += (
            f" That only covers {units_sourced} of {units_needed} units - place a "
            f"purchase order for the remaining {remaining} units."
        )
        recommended_action = f"{action}_and_purchase_order_remainder"
    else:
        recommended_action = action

    return StockSourcingPlan(
        sku=sku,
        units_needed=units_needed,
        candidates_considered=len(candidates),
        eligible_considered=len(eligible),
        selected_source_type=best.source_type,
        selected_source_id=best.source_id,
        units_sourced=units_sourced,
        remaining_gap_units=remaining,
        recommended_action=recommended_action,
        conclusion=conclusion,
        ranked=ranked_sources,
    )
