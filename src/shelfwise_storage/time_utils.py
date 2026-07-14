from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    Single source of truth for the `_now()` helper independently redefined across
    shelfwise_connectors, shelfwise_mlops, shelfwise_worldgen, and shelfwise_action.
    """
    return datetime.now(UTC).isoformat()
