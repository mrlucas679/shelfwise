from __future__ import annotations

from fastapi.testclient import TestClient

import shelfwise_backend.app as app_module
from shelfwise_backend.agentic_cascade import AgenticCascadeError


def test_agentic_provider_details_are_logged_not_returned_to_clients(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AgenticCascadeError("provider https://secret.example/v1 failed with token=hidden")

    monkeypatch.setattr(app_module, "run_golden_cascade_via_agents", fail)

    response = TestClient(app_module.app).post("/demo/golden/agentic")

    assert response.status_code == 503
    assert response.json()["detail"] == "Live agentic inference is unavailable"
    assert "secret.example" not in response.text
    assert "token" not in response.text
