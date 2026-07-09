from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, decision_store, learning_store, tool_audit
from shelfwise_backend.tools.mcp_surface import (
    PlatformTool,
    build_platform_tools,
    register_platform_mcp,
)


def test_trace_endpoint_returns_recorded_demo_trace() -> None:
    client = TestClient(app)
    run = client.get("/demo/golden")
    correlation_id = run.json()["correlation_id"]

    response = client.get(f"/trace/{correlation_id}")
    missing = client.get("/trace/missing")

    assert response.status_code == 200
    trace = response.json()["trace"]
    assert trace["correlation_id"] == correlation_id
    assert trace["decision_id"] == run.json()["decision"]["id"]
    assert "inventory" in trace["evidence_agents"]
    assert trace["spans"]
    assert missing.status_code == 404


def test_chat_is_bounded_streaming_and_write_guarded(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("API_KEY", "secret")

    blocked = client.post("/chat", json={"question": "what is at risk?"})
    allowed = client.post(
        "/chat",
        json={"question": "what is at risk?"},
        headers={"x-api-key": "secret"},
    )
    too_long = client.post(
        "/chat",
        json={"question": "x" * 2_001},
        headers={"x-api-key": "secret"},
    )

    assert blocked.status_code == 401
    assert allowed.status_code == 200
    assert "ShelfWise" in allowed.text
    assert too_long.status_code == 422


def test_platform_tools_are_read_only_grounded_and_audited() -> None:
    client = TestClient(app)
    decision = client.get("/demo/golden").json()["decision"]
    tools = {
        tool.name: tool
        for tool in build_platform_tools(
            decisions=decision_store,
            memory=learning_store,
            audit=tool_audit,
        )
    }

    stock = asyncio.run(tools["get_stock"].fn(sku="4011"))
    open_decisions = asyncio.run(tools["list_open_decisions"].fn())
    explanation = asyncio.run(tools["explain_decision"].fn(decision_id=decision["id"]))
    simulation = asyncio.run(tools["simulate_markdown"].fn(sku="4011", discount_pct=0.2))

    assert all(tool.read_only for tool in tools.values())
    assert stock["on_hand"] == 240
    assert any(item["id"] == decision["id"] for item in open_decisions["decisions"])
    assert explanation["critic_verdict"] == "approved"
    assert simulation["incremental_profit"]["minor_units"] > 0
    assert {event["tool"] for event in tool_audit.list()} >= {
        "get_stock",
        "list_open_decisions",
        "explain_decision",
        "simulate_markdown",
    }


def test_platform_tool_listing_endpoint_exposes_no_write_tools() -> None:
    client = TestClient(app)

    response = client.get("/tools/platform")

    assert response.status_code == 200
    tools = response.json()["tools"]
    assert tools
    assert all(tool["read_only"] for tool in tools)
    assert {tool["name"] for tool in tools} >= {"get_stock", "simulate_markdown"}


def test_register_platform_mcp_refuses_write_capable_tools() -> None:
    async def noop() -> dict:
        return {}

    class FakeMcp:
        def tool(self, **kwargs):
            _ = kwargs

            def decorator(fn):
                return fn

            return decorator

    register_platform_mcp(
        FakeMcp(),
        [PlatformTool("get_stock", "read", True, noop)],
    )
    try:
        register_platform_mcp(
            FakeMcp(),
            [PlatformTool("apply_markdown", "write", False, noop)],
        )
    except ValueError as exc:
        assert "refusing to expose write-capable" in str(exc)
    else:
        raise AssertionError("write-capable tool should not be registered")
