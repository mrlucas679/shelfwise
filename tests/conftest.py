from __future__ import annotations

import pytest

from shelfwise_backend.app import decision_store, learning_store


@pytest.fixture(autouse=True)
def _reset_demo_stores() -> None:
    """decision_store/learning_store are process-wide singletons on shelfwise_backend.app.

    Decision ids are now deterministic per scenario (shelfwise_backend.cascade) instead of
    random per call - that's the fix for the duplicate-decision bug, not an oversight. It does
    mean repeated calls within a single process resolve to the SAME record, so tests need a
    clean slate each time to stay isolated from one another.
    """
    decision_store.clear()
    learning_store.clear()
