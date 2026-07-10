from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from shelfwise_contracts import Event

from .scenarios import build


@dataclass(frozen=True, slots=True)
class Frame:
    event: Event
    decisions: list
    context: dict


async def direct(
    scenario_id: str,
    *,
    run_one: Callable[[Event, dict], Awaitable[list]],
    seed_override: int | None = None,
) -> list[Frame]:
    """Drive a scenario through a supplied one-event pipeline adapter."""
    world, schedule = build(scenario_id, seed_override=seed_override)
    frames: list[Frame] = []
    for event in world.run():
        context = {"scenario_id": scenario_id, "schedule": schedule, "synthetic": True}
        decisions = await run_one(event, context)
        frames.append(Frame(event=event, decisions=decisions, context=context))
    return frames
