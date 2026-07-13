"""Streaming synthetic fleet state and deterministic expiry-exception scoring."""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from heapq import heappush, heapreplace

from shelfwise_contracts import Money
from shelfwise_decision_science import score_expiry_risk

from .catalog.generate import generate_catalog


@dataclass(frozen=True, slots=True)
class FleetBatchState:
    """One product-location-lot row supplied to the cheap fleet scoring pass."""

    sku: str
    location_id: str
    lot_id: str
    units_on_hand: int
    days_to_expiry: int
    forecast_daily_units: Decimal
    unit_cost: Money
    cold_chain_risk: Decimal


@dataclass(frozen=True, slots=True)
class FleetExpiryCandidate:
    """A ranked deterministic exception; agentic review happens only after this stage."""

    sku: str
    location_id: str
    lot_id: str
    risk: Decimal
    zar_at_risk: Money
    days_to_expiry: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "location_id": self.location_id,
            "lot_id": self.lot_id,
            "risk": str(self.risk),
            "zar_at_risk": self.zar_at_risk.to_dict(),
            "days_to_expiry": self.days_to_expiry,
        }


@dataclass(frozen=True, slots=True)
class FleetScoreSummary:
    """Bounded receipt for a complete streamed scoring run."""

    rows_processed: int
    chunks_processed: int
    candidates_crossing_threshold: int
    total_zar_at_risk: Money
    top_candidates: tuple[FleetExpiryCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rows_processed": self.rows_processed,
            "chunks_processed": self.chunks_processed,
            "candidates_crossing_threshold": self.candidates_crossing_threshold,
            "total_zar_at_risk": self.total_zar_at_risk.to_dict(),
            "top_candidates": [candidate.to_dict() for candidate in self.top_candidates],
        }


def iter_fleet_batch_states(seed: int, *, locations: int = 40) -> Iterator[FleetBatchState]:
    """Stream one deterministic batch state for every SKU in the 500k fleet profile."""
    if locations <= 0:
        raise ValueError("locations must be positive")
    for index, product in enumerate(generate_catalog(seed, scale="fleet"), start=1):
        local_seed = zlib.crc32(f"{seed}:{product.sku}:batch".encode())
        units = 10 + local_seed % 91
        days = 1 + (local_seed // 97) % 45
        forecast = Decimal(1 + (local_seed // 101) % 24)
        cold_risk = Decimal((local_seed // 149) % 101) / Decimal("100")
        yield FleetBatchState(
            sku=product.sku,
            location_id=f"store_{(index - 1) % locations + 1:03d}",
            lot_id=f"LOT-{product.sku}-01",
            units_on_hand=units,
            days_to_expiry=days,
            forecast_daily_units=forecast,
            unit_cost=Money.zar(Decimal(product.price_cents) * Decimal("0.65") / Decimal("100")),
            cold_chain_risk=cold_risk,
        )


def score_fleet_expiry(
    rows: Iterable[FleetBatchState],
    *,
    chunk_size: int = 1_000,
    risk_threshold: Decimal = Decimal("0.60"),
    top_limit: int = 200,
) -> FleetScoreSummary:
    """Score batch rows in a bounded streaming pass and retain only reviewable exceptions."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not Decimal("0") <= risk_threshold <= Decimal("1"):
        raise ValueError("risk_threshold must be between 0 and 1")
    if top_limit <= 0:
        raise ValueError("top_limit must be positive")

    processed = candidates = total_minor = 0
    heap: list[tuple[Decimal, int, int, FleetExpiryCandidate]] = []
    for sequence, row in enumerate(rows, start=1):
        result = score_expiry_risk(
            sku=row.sku,
            units_on_hand=Decimal(row.units_on_hand),
            days_to_expiry=Decimal(row.days_to_expiry),
            forecast_daily_units=row.forecast_daily_units,
            unit_cost=row.unit_cost,
            cold_chain_risk=row.cold_chain_risk,
            cold_chain_penalty_days=row.cold_chain_risk * Decimal("2"),
        )
        processed += 1
        if result.risk < risk_threshold:
            continue
        candidates += 1
        total_minor += result.zar_at_risk.minor_units
        candidate = FleetExpiryCandidate(
            sku=row.sku,
            location_id=row.location_id,
            lot_id=row.lot_id,
            risk=result.risk,
            zar_at_risk=result.zar_at_risk,
            days_to_expiry=row.days_to_expiry,
        )
        priority = (candidate.risk, candidate.zar_at_risk.minor_units, sequence, candidate)
        if len(heap) < top_limit:
            heappush(heap, priority)
        elif priority[:3] > heap[0][:3]:
            heapreplace(heap, priority)

    ranked = tuple(
        item[3] for item in sorted(heap, key=lambda item: item[:3], reverse=True)
    )
    return FleetScoreSummary(
        rows_processed=processed,
        chunks_processed=(processed + chunk_size - 1) // chunk_size,
        candidates_crossing_threshold=candidates,
        total_zar_at_risk=Money(total_minor),
        top_candidates=ranked,
    )
