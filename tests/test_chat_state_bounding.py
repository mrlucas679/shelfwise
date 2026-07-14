"""Regression test for the unbounded /chat state payload found during a 15-minute,
145-cycle live full-system run: decisions and learning events grew without bound as the
store accumulated history, eventually pushing prompt latency past LLM_TIMEOUT_SECONDS and
silently falling back to the offline reply for the rest of the run (only 2 of 49 chat
calls got a real model answer). The decision store remains complete, while prompt context
uses a bounded recent window plus aggregate counts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import (
    _CHAT_LEARNING_EVENT_LIMIT,
    _CHAT_PENDING_DECISION_LIMIT,
    _CHAT_RESOLVED_DECISION_LIMIT,
    _bounded_chat_decisions,
    _bounded_recent,
    app,
)


def _decision(idx: int, *, status: str) -> dict[str, object]:
    return {
        "id": f"dec_{idx}",
        "status": status,
        "created_at": f"2026-07-10T00:{idx:02d}:00+00:00",
        "updated_at": f"2026-07-10T00:{idx:02d}:00+00:00",
    }


def _decision_with_context(
    idx: int,
    *,
    status: str,
    summary: str,
    risk_tier: str = "low",
) -> dict[str, object]:
    decision = _decision(idx, status=status)
    decision.update(
        {
            "summary": summary,
            "action": {"type": "monitor", "risk_tier": risk_tier},
        }
    )
    return decision


def test_bounded_chat_decisions_windows_pending_queue_by_recency() -> None:
    decisions = [_decision(i, status="pending") for i in range(50)]
    decisions += [_decision(i, status="approved") for i in range(50, 60)]

    bounded = _bounded_chat_decisions(decisions)

    pending_ids = {item["id"] for item in bounded if item["status"] == "pending"}
    assert len(pending_ids) == _CHAT_PENDING_DECISION_LIMIT
    assert pending_ids == {
        f"dec_{i}" for i in range(50 - _CHAT_PENDING_DECISION_LIMIT, 50)
    }


def test_bounded_chat_decisions_windows_resolved_history_by_recency() -> None:
    resolved_count = _CHAT_RESOLVED_DECISION_LIMIT + 20
    decisions = [_decision(i, status="approved") for i in range(resolved_count)]

    bounded = _bounded_chat_decisions(decisions)

    assert len(bounded) == _CHAT_RESOLVED_DECISION_LIMIT
    kept_indices = {int(item["id"].removeprefix("dec_")) for item in bounded}
    most_recent = set(range(resolved_count - _CHAT_RESOLVED_DECISION_LIMIT, resolved_count))
    assert kept_indices == most_recent


def test_bounded_chat_decisions_keeps_question_matching_history() -> None:
    decisions = [
        _decision_with_context(
            0,
            status="approved",
            summary="Supplier switch for chilled milk was approved",
            risk_tier="high",
        )
    ]
    decisions.extend(
        _decision_with_context(
            index,
            status="approved",
            summary="Routine shelf replenishment completed",
        )
        for index in range(1, 12)
    )

    bounded = _bounded_chat_decisions(
        decisions,
        question="What happened with milk supplier switch?",
    )

    assert any(item["id"] == "dec_0" for item in bounded)


def test_bounded_chat_decisions_keeps_high_risk_history_when_question_is_broad() -> None:
    decisions = [
        _decision_with_context(
            0,
            status="approved",
            summary="Older high-risk markdown recommendation",
            risk_tier="high",
        )
    ]
    decisions.extend(
        _decision_with_context(
            index,
            status="approved",
            summary="Routine low-risk replenishment",
        )
        for index in range(1, 12)
    )

    bounded = _bounded_chat_decisions(decisions, question="What needs attention?")

    assert any(item["id"] == "dec_0" for item in bounded)


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


def test_live_required_chat_rejects_offline_fallback(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    response = TestClient(app).post(
        "/chat",
        json={"question": "What needs attention?", "live_required": True},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Live chat inference failed"
