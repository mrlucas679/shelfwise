from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app


def test_connector_poll_status_route_reports_disabled_by_default() -> None:
    client = TestClient(app)

    response = client.get("/connectors/poll/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert isinstance(body["configured_systems"], list)
