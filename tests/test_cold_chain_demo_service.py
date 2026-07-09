from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.cold_chain_demo import ColdChainDemoService


def test_cold_chain_demo_service_stays_idle_when_disabled(monkeypatch) -> None:
    async def runner(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("runner should not start when disabled")

    service = ColdChainDemoService(feed_runner=runner)
    monkeypatch.delenv("COLD_CHAIN_DEMO", raising=False)

    asyncio.run(service.start())

    assert service.status()["enabled"] is False
    assert service.status()["running"] is False


def test_cold_chain_demo_service_starts_records_and_cancels(monkeypatch) -> None:
    ready = asyncio.Event()

    async def runner(publish, **kwargs):
        _ = kwargs
        await publish("fridge", {"asset_id": "fridge_dairy_1", "synthetic": True})
        ready.set()
        await asyncio.Event().wait()

    async def run() -> ColdChainDemoService:
        service = ColdChainDemoService(feed_runner=runner)
        monkeypatch.setenv("COLD_CHAIN_DEMO", "true")
        await service.start()
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert service.status()["running"] is True
        assert service.list_events()[0]["kind"] == "fridge"
        await service.stop()
        return service

    service = asyncio.run(run())

    assert service.status()["running"] is False


def test_cold_chain_feed_endpoint_exposes_buffered_messages() -> None:
    client = TestClient(app)

    response = client.get("/cold-chain/feed")

    assert response.status_code == 200
    body = response.json()
    assert "status" in body
    assert "events" in body
