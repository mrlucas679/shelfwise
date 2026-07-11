"""Regression test for the unbounded /chat state payload found during a 15-minute,
145-cycle live full-system run: decisions and learning events grew without bound as the
store accumulated history, eventually pushing prompt latency past LLM_TIMEOUT_SECONDS and
silently falling back to the offline reply for the rest of the run (only 2 of 49 chat
calls got a real model answer). Pending decisions must never be dropped - only resolved
history and learning events get windowed.
"""

from __future__ import annotations

from shelfwise_backend.app import (
    _CHAT_LEARNING_EVENT_LIMIT,
    _CHAT_RESOLVED_DECISION_LIMIT,
    _bounded_chat_decisions,
    _bounded_recent,
)


def _decision(idx: int, *, status: str) -> dict[str, object]:
    return {
        "id": f"dec_{idx}",
        "status": status,
        "created_at": f"2026-07-10T00:{idx:02d}:00+00:00",
        "updated_at": f"2026-07-10T00:{idx:02d}:00+00:00",
    }


def test_bounded_chat_decisions_keeps_every_pending_decision() -> None:
    decisions = [_decision(i, status="pending") for i in range(50)]
    decisions += [_decision(i, status="approved") for i in range(50, 60)]

    bounded = _bounded_chat_decisions(decisions)

    pending_ids = {item["id"] for item in bounded if item["status"] == "pending"}
    assert pending_ids == {f"dec_{i}" for i in range(50)}


def test_bounded_chat_decisions_windows_resolved_history_by_recency() -> None:
    resolved_count = _CHAT_RESOLVED_DECISION_LIMIT + 20
    decisions = [_decision(i, status="approved") for i in range(resolved_count)]

    bounded = _bounded_chat_decisions(decisions)

    assert len(bounded) == _CHAT_RESOLVED_DECISION_LIMIT
    kept_indices = {int(item["id"].removeprefix("dec_")) for item in bounded}
    most_recent = set(range(resolved_count - _CHAT_RESOLVED_DECISION_LIMIT, resolved_count))
    assert kept_indices == most_recent


def test_bounded_recent_is_a_no_op_under_the_limit() -> None:
    items = [{"created_at": "2026-07-10T00:00:00+00:00"}] * 5
    assert _bounded_recent(items, limit=_CHAT_LEARNING_EVENT_LIMIT) == items


def test_bounded_recent_caps_and_sorts_learning_events() -> None:
    events = [
        {"id": f"evt_{i}", "created_at": f"2026-07-10T00:{i:02d}:00+00:00"}
        for i in range(_CHAT_LEARNING_EVENT_LIMIT + 10)
    ]

    bounded = _bounded_recent(events, limit=_CHAT_LEARNING_EVENT_LIMIT)

    assert len(bounded) == _CHAT_LEARNING_EVENT_LIMIT
    assert bounded[0]["id"] == f"evt_{_CHAT_LEARNING_EVENT_LIMIT + 9}"
