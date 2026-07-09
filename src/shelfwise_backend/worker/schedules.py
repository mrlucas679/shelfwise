from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .plans import Plan, PlanRunner

_LOG = logging.getLogger("shelfwise.schedules")


@dataclass(slots=True)
class Schedule:
    name: str
    every_s: float
    make_plan: Callable[[], Plan]
    enabled: bool = True
    last_started: float | None = None
    running: bool = False


class Scheduler:
    """Minimal interval scheduler for journaled plans."""

    def __init__(self, runner: PlanRunner, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._runner = runner
        self._clock = clock
        self._schedules: dict[str, Schedule] = {}

    def add(self, schedule: Schedule) -> None:
        if schedule.name in self._schedules:
            raise ValueError(f"schedule exists: {schedule.name}")
        schedule.every_s = max(1.0, schedule.every_s)
        self._schedules[schedule.name] = schedule

    def due(self) -> list[Schedule]:
        now = self._clock()
        return [
            schedule
            for schedule in self._schedules.values()
            if schedule.enabled
            and not schedule.running
            and (
                schedule.last_started is None
                or now - schedule.last_started >= schedule.every_s
            )
        ]

    async def tick(self) -> int:
        """Run every due schedule once and skip overlapping runs."""
        fired = 0
        for schedule in self.due():
            schedule.running = True
            schedule.last_started = self._clock()
            try:
                result = await self._runner.run(schedule.make_plan())
                if result.status != "done":
                    _LOG.warning("schedule %s failed at %s", schedule.name, result.failed_step)
            except Exception:
                _LOG.exception("schedule %s crashed", schedule.name)
            finally:
                schedule.running = False
            fired += 1
        return fired

    async def run_forever(self, *, poll_s: float = 1.0) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(poll_s)
