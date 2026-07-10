from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_resilience.feed import run_demo_feed

Publish = Callable[[str, dict[str, Any]], Awaitable[None]]
FeedRunner = Callable[..., Awaitable[None]]


class ColdChainDemoService:
    """Optional lifespan service that replays synthetic cold-chain drill messages."""

    def __init__(
        self,
        *,
        feed_runner: FeedRunner = run_demo_feed,
        max_events: int = 500,
    ) -> None:
        self._feed_runner = feed_runner
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._lock = Lock()
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        """Start the background feed only when COLD_CHAIN_DEMO=true."""

        if not _enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._last_error = None
        self._task = asyncio.create_task(self._run(), name="shelfwise-cold-chain-demo")

    async def stop(self) -> None:
        """Cancel the background feed cleanly during app shutdown."""

        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Record one fridge/cold-chain feed message."""

        event = {"kind": kind, "data": deepcopy(data)}
        with self._lock:
            self._events.append(event)

    def list_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent feed messages newest-first."""

        bounded = max(1, min(limit, 500))
        with self._lock:
            return [deepcopy(event) for event in list(reversed(self._events))[:bounded]]

    def clear(self) -> None:
        """Clear buffered messages between tests or local demo runs."""

        with self._lock:
            self._events.clear()
        self._last_error = None

    def status(self) -> dict[str, Any]:
        """Expose service state without leaking task internals."""

        task = self._task
        running = task is not None and not task.done()
        return {
            "enabled": _enabled(),
            "running": running,
            "events_buffered": len(self._events),
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        try:
            await self._feed_runner(
                self.publish,
                interval_s=_float_env("COLD_CHAIN_DEMO_INTERVAL_S", 2.0),
                minutes=_int_env("COLD_CHAIN_DEMO_MINUTES", 60),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc)[:200]


def _enabled() -> bool:
    return os.getenv("COLD_CHAIN_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default
