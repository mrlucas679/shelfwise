"""Age-based retention for simulation-domain history ("Things..." item 7, final piece).

Opt-in and deliberately narrow: prunes ONLY `world_simulation`-domain rows - drill and
harness history whose lifecycle the data-lifecycle policy already declares disposable -
and never touches `operational_twin` rows, which are a real store's audit trail. Off by
default (`RETENTION_ENABLED`); the age floor cannot be configured below 7 days, so a
mis-set env var cannot silently eat yesterday's test evidence. Postgres-backend only:
unbounded table growth is a durable-storage problem, and the in-memory backend resets
with the process, so the service reports not-applicable there instead of pretending.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from shelfwise_storage import connect

_MIN_RETENTION_DAYS = 7.0
_SIMULATION_DOMAIN = "world_simulation"


def retention_enabled() -> bool:
    return os.getenv("RETENTION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def retention_days() -> float:
    raw = os.getenv("RETENTION_DAYS", "30").strip()
    try:
        return max(_MIN_RETENTION_DAYS, float(raw))
    except ValueError:
        return 30.0


def prune_simulation_history(*, database_url: str, now: datetime | None = None) -> dict[str, int]:
    """Delete aged simulation-domain rows; returns per-table counts for the receipt.

    Scope is exactly: events, resolved (approved/rejected) decisions, and cascade runs
    in the simulation domain, older than the retention window. Pending decisions are
    never pruned regardless of age - an unresolved recommendation is live work.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days())
    counts: dict[str, int] = {}
    with connect(database_url) as conn:
        counts["events"] = conn.execute(
            """
            with deleted as (
                delete from shelfwise_events
                where data_domain = %s and received_at < %s
                returning 1
            ) select count(*) from deleted
            """,
            (_SIMULATION_DOMAIN, cutoff),
        ).fetchone()["count"]
        counts["resolved_decisions"] = conn.execute(
            """
            with deleted as (
                delete from shelfwise_decisions
                where data_domain = %s and status in ('approved', 'rejected')
                  and updated_at < %s
                returning 1
            ) select count(*) from deleted
            """,
            (_SIMULATION_DOMAIN, cutoff),
        ).fetchone()["count"]
        counts["cascade_runs"] = conn.execute(
            """
            with deleted as (
                delete from cascade_runs
                where data_domain = %s and started_at < %s
                returning 1
            ) select count(*) from deleted
            """,
            (_SIMULATION_DOMAIN, cutoff),
        ).fetchone()["count"]
        conn.commit()
    return counts


class RetentionService:
    """Lifespan service: prune aged simulation history on a daily cadence."""

    def __init__(self, *, interval_s: float = 86_400.0, poll_s: float = 1.0) -> None:
        self._interval_s = max(3600.0, interval_s)
        self._poll_s = max(0.05, poll_s)
        self._task: asyncio.Task | None = None
        self._runs = 0
        self._last_counts: dict[str, int] = {}
        self._last_status = "idle"
        self._last_error: str | None = None
        self._refused_reason: str | None = None
        self._next_at = 0.0

    async def start(self) -> None:
        if not retention_enabled():
            return
        if os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower() != "postgres":
            self._refused_reason = (
                "retention applies to durable Postgres storage; the in-memory backend "
                "resets with the process, so there is nothing honest to prune"
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="shelfwise-retention")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    def status(self) -> dict[str, Any]:
        task = self._task
        return {
            "enabled": retention_enabled(),
            "running": task is not None and not task.done(),
            "refused_reason": self._refused_reason,
            "retention_days": retention_days(),
            "domain": _SIMULATION_DOMAIN,
            "runs": self._runs,
            "last_counts": dict(self._last_counts),
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    def run_once(self) -> dict[str, int]:
        counts = prune_simulation_history(database_url=os.getenv("DATABASE_URL", ""))
        self._runs += 1
        self._last_counts = counts
        self._last_status = "ok"
        return counts

    async def _run(self) -> None:
        import time

        while True:
            try:
                now = time.monotonic()
                if now >= self._next_at:
                    self._next_at = now + self._interval_s
                    await asyncio.to_thread(self.run_once)
                await asyncio.sleep(self._poll_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_status = "crashed"
                self._last_error = str(exc)[:200]
                await asyncio.sleep(self._poll_s)
