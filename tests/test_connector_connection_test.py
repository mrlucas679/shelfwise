from __future__ import annotations

import asyncio

import httpx
import pytest

from shelfwise_connectors import SourceSystem
from shelfwise_connectors.connector_test import (
    ConnectorTestNotSupported,
    build_test_connector,
)
from shelfwise_connectors.connector_test import test_connection as run_connection_test

_SAP_FIELDS = {"base_url": "https://sap.example.com", "token": "tok"}


def _run_with_fake_fetch_page(monkeypatch: pytest.MonkeyPatch, fake_fetch_page):
    connector = build_test_connector(SourceSystem.SAP, _SAP_FIELDS)
    monkeypatch.setattr(connector, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(
        "shelfwise_connectors.connector_test.build_test_connector", lambda *a, **k: connector
    )
    return asyncio.run(run_connection_test(SourceSystem.SAP, _SAP_FIELDS))


def test_ok_result_on_successful_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_page(cursor: str | None) -> tuple[list, str | None]:
        return [], None

    result = _run_with_fake_fetch_page(monkeypatch, fake_fetch_page)

    assert result.status == "ok"
    assert result.to_dict()["ok"] is True


def test_missing_field_is_a_config_error_without_a_network_call() -> None:
    result = asyncio.run(
        run_connection_test(SourceSystem.SAP, {"base_url": "https://sap.example.com"})
    )

    assert result.status == "config_error"
    assert "token" in result.detail


def test_401_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_page(cursor: str | None) -> tuple[list, str | None]:
        request = httpx.Request("GET", "https://sap.example.com")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    result = _run_with_fake_fetch_page(monkeypatch, fake_fetch_page)

    assert result.status == "auth_error"


def test_404_maps_to_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_page(cursor: str | None) -> tuple[list, str | None]:
        request = httpx.Request("GET", "https://sap.example.com")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    result = _run_with_fake_fetch_page(monkeypatch, fake_fetch_page)

    assert result.status == "not_found"


def test_timeout_maps_to_timeout_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_page(cursor: str | None) -> tuple[list, str | None]:
        raise httpx.ConnectTimeout("timed out")

    result = _run_with_fake_fetch_page(monkeypatch, fake_fetch_page)

    assert result.status == "timeout"


def test_network_error_maps_to_network_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_page(cursor: str | None) -> tuple[list, str | None]:
        raise httpx.ConnectError("dns failure")

    result = _run_with_fake_fetch_page(monkeypatch, fake_fetch_page)

    assert result.status == "network_error"


def test_webhook_only_system_is_not_supported() -> None:
    with pytest.raises(ConnectorTestNotSupported):
        asyncio.run(run_connection_test(SourceSystem.SHOPIFY, {}))


def test_odoo_bad_uid_raises_value_error() -> None:
    with pytest.raises(ValueError):
        build_test_connector(
            SourceSystem.ODOO,
            {
                "base_url": "https://o.example.com",
                "database": "db",
                "uid": "not-a-number",
                "api_key": "x",
            },
        )
