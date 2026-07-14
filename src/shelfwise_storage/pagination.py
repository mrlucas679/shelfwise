from __future__ import annotations

DEFAULT_MAX_LIMIT = 500


def validate_limit(limit: int, *, max_limit: int = DEFAULT_MAX_LIMIT) -> None:
    """Reject a caller-supplied page/list `limit` outside `(0, max_limit]`.

    Single source of truth for the bounds check independently redefined across
    shelfwise_backend (candidate_store, event_store, open_orders), shelfwise_connectors
    (inbound_store), shelfwise_twin, and shelfwise_worldgen.
    """
    if limit <= 0 or limit > max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}")
