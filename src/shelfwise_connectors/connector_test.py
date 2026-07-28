"""Live connection test for one poll-based ERP/WMS system's credentials.

Answers the question a store owner actually has when connecting their shop: "did the
values I just typed in actually work?" Reuses each connector's real `fetch_page(None)` -
the same call the background poll loop makes - rather than a separate, divergent
"ping" implementation, so a passing test means the poll loop will also work. Does not
persist a cursor or ingest any record; this is a read-only probe of page one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from .canonical import SourceSystem
from .connectors.poll import InMemoryCursorStore, PollingConnector
from .connectors.systems.dynamics import DynamicsBusinessCentralInventoryConnector
from .connectors.systems.odoo import OdooProductConnector
from .connectors.systems.sap import SapS4InventoryConnector
from .connectors.systems.syspro import SysproInventoryConnector

ConnectionTestStatus = Literal[
    "ok", "auth_error", "not_found", "network_error", "timeout", "config_error", "unknown_error"
]

# Fields required for each poll-based system, and their types where not a plain string.
# Single source of truth for "what does this system need to connect" - the frontend
# fetches this shape from GET /connectors/systems instead of hard-coding it, and
# connector_poll_service._resolved_fields's env-var fallback keys must keep matching
# these field keys exactly.
CREDENTIAL_FIELDS: dict[SourceSystem, list[dict[str, object]]] = {
    SourceSystem.ODOO: [
        {"key": "base_url", "label": "Base URL"},
        {"key": "database", "label": "Database"},
        {"key": "uid", "label": "User ID"},
        {"key": "api_key", "label": "API key", "secret": True},
    ],
    SourceSystem.SAP: [
        {"key": "base_url", "label": "Base URL"},
        {"key": "token", "label": "API token", "secret": True},
    ],
    SourceSystem.SYSPRO: [
        {"key": "base_url", "label": "Base URL"},
        {"key": "token", "label": "API token", "secret": True},
    ],
    SourceSystem.DYNAMICS: [
        {"key": "base_url", "label": "Items collection URL"},
        {"key": "token", "label": "OAuth bearer token", "secret": True},
        {"key": "location_id", "label": "Location ID"},
    ],
}

TESTABLE_SYSTEMS = frozenset(CREDENTIAL_FIELDS)


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    status: ConnectionTestStatus
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "ok": self.status == "ok", "detail": self.detail}


class ConnectorTestNotSupported(ValueError):
    """Raised for a system with no live test (webhook-authenticated systems, CSV)."""


def _missing_fields(system: SourceSystem, fields: dict[str, str]) -> list[str]:
    required = CREDENTIAL_FIELDS.get(system, [])
    return [str(spec["key"]) for spec in required if not fields.get(str(spec["key"]), "").strip()]


def build_test_connector(system: SourceSystem, fields: dict[str, str]) -> PollingConnector:
    """Construct a throwaway connector instance for a one-shot `fetch_page(None)` probe."""
    cursors = InMemoryCursorStore()
    if system is SourceSystem.ODOO:
        try:
            uid = int(fields["uid"])
        except ValueError as exc:
            raise ValueError("User ID must be a number") from exc
        return OdooProductConnector(
            cursors,
            base_url=fields["base_url"],
            database=fields["database"],
            uid=uid,
            api_key=fields["api_key"],
            tenant_id="connection-test",
        )
    if system is SourceSystem.SAP:
        return SapS4InventoryConnector(
            cursors, base_url=fields["base_url"], token=fields["token"], tenant_id="connection-test"
        )
    if system is SourceSystem.SYSPRO:
        return SysproInventoryConnector(
            cursors, base_url=fields["base_url"], token=fields["token"], tenant_id="connection-test"
        )
    if system is SourceSystem.DYNAMICS:
        return DynamicsBusinessCentralInventoryConnector(
            cursors,
            base_url=fields["base_url"],
            token=fields["token"],
            location_id=fields["location_id"],
            tenant_id="connection-test",
        )
    raise ConnectorTestNotSupported(f"{system.value} has no live connection test")


async def test_connection(system: SourceSystem, fields: dict[str, str]) -> ConnectionTestResult:
    """Attempt one real page-one fetch against the source system with the given fields.

    Never raises for an expected failure mode (bad credentials, unreachable host, timeout) -
    those come back as a `ConnectionTestResult` so the caller can return them to the owner
    as a normal response rather than a 500. Programming errors (unsupported system) still
    raise, since those indicate a caller bug, not a live-environment condition.
    """
    if system not in TESTABLE_SYSTEMS:
        raise ConnectorTestNotSupported(f"{system.value} has no live connection test")
    missing = _missing_fields(system, fields)
    if missing:
        return ConnectionTestResult(
            "config_error",
            f"missing required field(s): {', '.join(missing)}",
        )
    try:
        connector = build_test_connector(system, fields)
    except ValueError as exc:
        return ConnectionTestResult("config_error", str(exc))
    try:
        await connector.fetch_page(None)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return ConnectionTestResult(
                "auth_error",
                "the source system rejected these credentials",
            )
        if code == 404:
            return ConnectionTestResult(
                "not_found",
                "the source system URL was reachable but returned 404",
            )
        return ConnectionTestResult("unknown_error", f"the source system returned HTTP {code}")
    except httpx.TimeoutException:
        return ConnectionTestResult("timeout", "the source system did not respond in time")
    except httpx.RequestError:
        return ConnectionTestResult(
            "network_error",
            "could not reach the source system - check the URL",
        )
    except (KeyError, TypeError, ValueError) as exc:
        return ConnectionTestResult("config_error", str(exc))
    return ConnectionTestResult("ok", "connected successfully")
