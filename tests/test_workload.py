from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shelfwise_backend.workload import assess_decision_sla, build_workload_snapshot


def _decision(created_at: datetime, *, risk: str = "high", role: str = "manager") -> dict:
    return {
        "id": "dec-1",
        "status": "pending",
        "role": role,
        "created_at": created_at.isoformat(),
        "action": {"risk_tier": risk},
    }


def test_high_risk_pending_decision_breaches_sla_and_escalates() -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)

    assessment = assess_decision_sla(
        _decision(now - timedelta(minutes=61)),
        now=now,
    )

    assert assessment is not None
    assert assessment.status == "breached"
    assert assessment.escalation_required is True


def test_workload_snapshot_reports_role_caps_and_suppression() -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    decisions = [
        _decision(now - timedelta(minutes=50), role="manager"),
        {**_decision(now - timedelta(minutes=10)), "id": "dec-2"},
    ]

    snapshot = build_workload_snapshot(
        decisions,
        candidates=[{"status": "suppressed"}],
        now=now,
    )

    assert snapshot["pending"] == 2
    assert snapshot["role_counts"] == {"manager": 2}
    assert snapshot["suppressed_candidates"] == 1
    assert snapshot["oldest_pending_age_seconds"] == 3_000


def test_non_pending_or_invalid_timestamp_is_not_assessed() -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)

    assert assess_decision_sla({"status": "approved"}, now=now) is None
    assert assess_decision_sla({"status": "pending", "created_at": "bad"}, now=now) is None


def test_sla_environment_override_is_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_SLA_HIGH_MINUTES", "5")
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)

    assessment = assess_decision_sla(_decision(now - timedelta(minutes=6)), now=now)

    assert assessment is not None
    assert assessment.sla_seconds == 300
