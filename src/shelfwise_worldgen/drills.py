from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from shelfwise_contracts import Event

from .play import direct


@dataclass(frozen=True, slots=True)
class DrillReport:
    scenario_id: str
    events_total: int
    decisions_total: int
    pending_total: int
    recovered_cents: int

    def summary(self) -> str:
        """Return a short operator-facing drill summary."""
        return (
            f"{self.scenario_id}: {self.events_total} events, "
            f"{self.decisions_total} decisions, {self.pending_total} pending"
        )


async def run_drill(
    scenario_id: str,
    *,
    run_one: Callable[[Event, dict], Awaitable[list]],
    seed_override: int | None = None,
) -> DrillReport:
    """Run a scenario and aggregate decision outcomes."""
    frames = await direct(scenario_id, run_one=run_one, seed_override=seed_override)
    decisions = [decision for frame in frames for decision in frame.decisions]
    return DrillReport(
        scenario_id=scenario_id,
        events_total=len(frames),
        decisions_total=len(decisions),
        pending_total=sum(1 for decision in decisions if _is_pending(decision)),
        recovered_cents=sum(_recovered_cents(decision) for decision in decisions),
    )


def _is_pending(decision: object) -> bool:
    """Detect pending decisions across dicts and simple objects."""
    if isinstance(decision, dict):
        return decision.get("status") == "pending"
    return getattr(decision, "status", None) == "pending"


def _recovered_cents(decision: object) -> int:
    """Extract recovered cents from a decision outcome when present."""
    if not isinstance(decision, dict):
        return 0
    outcome = decision.get("outcome") or {}
    money = outcome.get("rand_recovered") or {}
    return int(money.get("minor_units") or 0)
