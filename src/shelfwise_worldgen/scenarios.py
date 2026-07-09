from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .catalog.sample import sample_assortment
from .sa_ground_truth import load_shedding_schedule
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
    )
}


def build(
    scenario_id: str,
    *,
    seed_override: int | None = None,
    assortment_size: int | None = None,
    catalog_scale: str = "supermarket",
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
        area=scenario.area,
        stage=scenario.stage,
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
