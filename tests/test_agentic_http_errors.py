from __future__ import annotations

from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.agentic_cascade import AgenticCascadeDeadlineError, AgenticCascadeError


def test_agentic_provider_details_are_logged_not_returned_to_clients(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AgenticCascadeError("provider https://secret.example/v1 failed with token=hidden")

    monkeypatch.setattr(app_module, "run_golden_cascade_via_agents", fail)

    response = TestClient(app_module.app).post("/scenarios/golden/agentic")

    assert response.status_code == 503
    assert response.json()["detail"] == "Live agentic inference is unavailable"
    assert "secret.example" not in response.text
    assert "token" not in response.text


def test_agentic_deadline_exceeded_returns_structured_503_with_progress(monkeypatch) -> None:
    """A cascade that stops itself before the response deadline must tell the caller how
    far it got, instead of returning the same opaque unavailability message as a genuine
    provider failure (the 2026-07-14 forensic audit found no way to distinguish the two).
    """

    def deadline_hit(*args, **kwargs):
        raise AgenticCascadeDeadlineError(
            "live agentic cold-chain cascade could not finish inside the response deadline",
            completed_model_calls=1,
            elapsed_ms=9500,
        )

    monkeypatch.setattr(app_module, "run_cold_chain_cascade_via_agents", deadline_hit)

    response = TestClient(app_module.app).post("/scenarios/cold-chain/agentic")

    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["detail"] == "cascade could not finish inside the response deadline"
    assert body["completed_model_calls"] == 1
    assert body["elapsed_ms"] == 9500
