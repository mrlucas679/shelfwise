from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .play import Frame

_LOGGER = logging.getLogger("shelfwise.worldgen.narrate")


async def narrate(
    frame: Frame,
    *,
    headline: str,
    llm: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Add optional narration while remaining offline-safe."""
    prompt = (
        f"{headline}\n"
        f"Event {frame.event.type.value} for {frame.event.payload.get('sku', 'unknown')}.\n"
        "Describe the operational moment in one sentence."
    )
    if llm is None:
        return f"{headline}: {frame.event.type.value} event emitted."
    try:
        return (await llm(prompt)).strip()[:500]
    except Exception:
        # Narration is cosmetic and must never break the worldgen pipeline, but a silent
        # `except Exception: pass` here would hide real auth/timeout/programming failures
        # from the injected `llm` callable - log them, then fall back to the plain headline.
        _LOGGER.warning("narration failed, falling back to plain headline", exc_info=True)
        return f"{headline}: {frame.event.type.value} event emitted."
