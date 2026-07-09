from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_eval.autopilot import APPROVE, REJECT, SKIP, review_decision


def test_critic_approved_pending_decision_is_approved() -> None:
    verdict = review_decision(
        {"status": "pending", "critic_verdict": "approved", "expected_outcome": {}}
    )
    assert verdict["action"] == APPROVE
    assert verdict["reviewer"] == "autopilot"


def test_small_exposure_review_is_approved_and_large_is_rejected() -> None:
    small = review_decision(
        {
            "status": "pending",
            "critic_verdict": "review_required",
            "expected_outcome": {"revenue_exposure_minor_units": 4_500},
        }
    )
    large = review_decision(
        {
            "status": "pending",
            "critic_verdict": "review_required",
            "expected_outcome": {"revenue_exposure_minor_units": -350_000},
        }
    )
    assert small["action"] == APPROVE
    assert large["action"] == REJECT
    assert large["exposure_minor_units"] == 350_000


def test_resolved_decisions_are_skipped_and_unknown_verdicts_rejected() -> None:
    resolved = review_decision({"status": "approved", "critic_verdict": "approved"})
    unknown = review_decision({"status": "pending"})
    assert resolved["action"] == SKIP
    assert unknown["action"] == REJECT


def test_autopilot_drives_the_real_hitl_endpoints_end_to_end() -> None:
    client = TestClient(app)
    golden = client.get("/demo/golden")
    decision = golden.json()["decision"]
    verdict = review_decision(decision)
    assert verdict["action"] == APPROVE

    response = client.post(f"/decisions/{decision['id']}/{verdict['action']}")

    assert response.status_code == 200
    assert response.json()["decision"]["status"] == "approved"
    assert response.json()["learning_event"] is not None
