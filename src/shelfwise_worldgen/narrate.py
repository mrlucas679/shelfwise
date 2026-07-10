from __future__ import annotations

from collections.abc import Awaitable, Callable

from .play import Frame


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
        return f"{headline}: {frame.event.type.value} event emitted."
