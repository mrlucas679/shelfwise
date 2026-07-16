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


def test_connector_poll_status_route_reports_a_real_run_when_enabled(monkeypatch) -> None:
    """The status route must reflect genuine service state after real activity, not just
    the never-enabled default - a broken 'running' payload was previously invisible
    because only the disabled state was ever asserted."""
    from shelfwise_backend import app as app_module

    monkeypatch.setenv("CONNECTOR_POLL_ENABLED", "true")

    import asyncio

    pulled = asyncio.run(app_module.connector_poll_service.run_once())

    client = TestClient(app)
    response = client.get("/connectors/poll/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["runs"] >= 1
    assert body["last_status"] == "ok"
    assert body["records_pulled"] >= pulled >= 0
    assert body["interval_s"] >= 5.0, "hot-loop floor must be visible in the status payload"
