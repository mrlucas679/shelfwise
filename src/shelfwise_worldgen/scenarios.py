from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .catalog.sample import sample_assortment
from .world import World, WorldConfig


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    seed: int
    start: date
    days: int
    area: str
    stage: int
    incident_days: tuple[int, ...]
    description: str


SCENARIOS: dict[str, Scenario] = {
    "stage4_payday_coldchain": Scenario(
        id="stage4_payday_coldchain",
        seed=42,
        start=date(2026, 6, 22),
        days=7,
        area="observatory_blk7",
        stage=4,
        incident_days=(1, 2, 3),
        description="Stage 4 outage pressure during payday week with refrigerated stock exposed.",
    ),
    "stage2_midmonth_lull": Scenario(
        id="stage2_midmonth_lull",
        seed=77,
        start=date(2026, 6, 8),
        days=7,
        area="observatory_blk7",
        stage=2,
        incident_days=(4,),
        description="Mild stage 2 week in the mid-month demand lull; routine ops discipline.",
    ),
    "stage6_blackout_weekend": Scenario(
        id="stage6_blackout_weekend",
        seed=133,
        start=date(2026, 6, 26),
        days=5,
        area="observatory_blk7",
        stage=6,
        incident_days=(0, 1, 2),
        description=(
            "Severe stage 6 stretch over a payday weekend; cold chain under maximum pressure."
        ),
    ),
}


def build(
    scenario_id: str,
    *,
    seed_override: int | None = None,
    assortment_size: int | None = None,
    catalog_scale: str = "supermarket",
    tenant_id: str = "local",
) -> tuple[World, list[dict]]:
    """Build a world plus external schedule labels for a named scenario.

    By default the world runs against the small, hand-curated ground-truth product
    list. Passing `assortment_size` swaps in a deterministic, realistic slice of the
    full generated supermarket catalog instead (see `catalog.sample.sample_assortment`),
    so the same scenario mechanics can be stress-tested across every department in the
    store rather than just the hero SKUs.
    """
    scenario = SCENARIOS[scenario_id]
    seed = scenario.seed if seed_override is None else seed_override
    products = (
        sample_assortment(seed, size=assortment_size, scale=catalog_scale)
        if assortment_size is not None
        else None
    )
    cfg = WorldConfig(
        seed=seed,
        start=scenario.start,
        days=scenario.days,
        scenario_id=scenario.id,
        area=scenario.area,
        stage=scenario.stage,
        incident_days=scenario.incident_days,
        tenant_id=tenant_id,
        products=products,
    )
    schedule = [
        row
        for row in load_shedding_schedule(
            seed,
            area=scenario.area,
            start=scenario.start,
            days=scenario.days,
            stage=scenario.stage,
        )
        if row["day_index"] in scenario.incident_days
    ]
    return World(cfg), schedule


def load_shedding_schedule(
    seed: int,
    *,
    area: str,
    start: date,
    days: int,
    stage: int,
) -> list[dict[str, object]]:
    """Generate labeled outage windows without importing legacy product fixtures."""
    schedule: list[dict[str, object]] = []
    for day_index in range(days):
        current = start + timedelta(days=day_index)
        for slot_index in range(max(0, min(6, stage))):
            raw = f"{seed}:{area}:{current.isoformat()}:{slot_index}".encode()
            offset = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")
            hour = (offset % 11) * 2
            begins = datetime.combine(current, time(hour=hour))
            schedule.append(
                {
                    "area": area,
                    "stage": stage,
                    "day_index": day_index,
                    "start": begins.isoformat(),
                    "end": (begins + timedelta(hours=2)).isoformat(),
                    "synthetic": True,
                }
            )
    return sorted(schedule, key=lambda row: str(row["start"]))
