"""Background polling for ERP/WMS connectors that use pull, not webhook, transport.

Wires the already-built `PollingConnector.pull()` cursor/dedup machinery to the same
ingestion pipeline every webhook/CSV/manual intake route already uses (injected as
`process_record`, so this module has no dependency on `app.py`), on an interval, for the
single tenant this deployment is configured for.

Per-tenant credential storage for real multi-tenant ERP connections is a deliberate,
explicit non-goal here - see `.env.example`. This deployment is single-tenant
(`SHELFWISE_TENANT_ID`), matching how `LLM_ROUTINE_BASE_URL` etc. are already configured,
so credentials are read from environment variables per system rather than a new
credentials table. A system is polled only when its env vars are completely set; a
partially-configured system is treated as not configured, never as a broken poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from shelfwise_connectors import (
    CursorStore,
    DynamicsBusinessCentralInventoryConnector,
    OdooProductConnector,
    PollingConnector,
    SapS4InventoryConnector,
    SysproInventoryConnector,
)

_LOG = logging.getLogger("shelfwise.connector_poll")

ProcessRecord = Callable[[Any], dict[str, Any]]


def connector_poll_enabled() -> bool:
    return os.getenv("CONNECTOR_POLL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def build_configured_connectors(*, cursors: CursorStore, tenant_id: str) -> list[PollingConnector]:
    """Construct one connector per polling system that has complete env credentials."""
    connectors: list[PollingConnector] = []

    odoo_base = os.getenv("SHELFWISE_CONNECTOR_ODOO_BASE_URL", "").strip()
    odoo_database = os.getenv("SHELFWISE_CONNECTOR_ODOO_DATABASE", "").strip()
    odoo_uid = os.getenv("SHELFWISE_CONNECTOR_ODOO_UID", "").strip()
    odoo_api_key = os.getenv("SHELFWISE_CONNECTOR_ODOO_API_KEY", "").strip()
    if odoo_base and odoo_database and odoo_uid and odoo_api_key:
        try:
            connectors.append(
                OdooProductConnector(
                    cursors,
                    base_url=odoo_base,
                    database=odoo_database,
                    uid=int(odoo_uid),
                    api_key=odoo_api_key,
                    tenant_id=tenant_id,
                )
            )
        except ValueError:
            _LOG.warning("SHELFWISE_CONNECTOR_ODOO_UID is not an integer - Odoo poll disabled")

    sap_base = os.getenv("SHELFWISE_CONNECTOR_SAP_BASE_URL", "").strip()
    sap_token = os.getenv("SHELFWISE_CONNECTOR_SAP_TOKEN", "").strip()
    if sap_base and sap_token:
        connectors.append(
            SapS4InventoryConnector(
                cursors, base_url=sap_base, token=sap_token, tenant_id=tenant_id
            )
        )

    syspro_base = os.getenv("SHELFWISE_CONNECTOR_SYSPRO_BASE_URL", "").strip()
    syspro_token = os.getenv("SHELFWISE_CONNECTOR_SYSPRO_TOKEN", "").strip()
    if syspro_base and syspro_token:
        connectors.append(
            SysproInventoryConnector(
                cursors, base_url=syspro_base, token=syspro_token, tenant_id=tenant_id
            )
        )

    dynamics_base = os.getenv("SHELFWISE_CONNECTOR_DYNAMICS_BASE_URL", "").strip()
    dynamics_token = os.getenv("SHELFWISE_CONNECTOR_DYNAMICS_TOKEN", "").strip()
    dynamics_location = os.getenv("SHELFWISE_CONNECTOR_DYNAMICS_LOCATION_ID", "").strip()
    if dynamics_base and dynamics_token and dynamics_location:
        connectors.append(
            DynamicsBusinessCentralInventoryConnector(
                cursors,
                base_url=dynamics_base,
                token=dynamics_token,
                location_id=dynamics_location,
                tenant_id=tenant_id,
            )
        )

    return connectors


class ConnectorPollService:
    """Optional lifespan-managed loop that pulls configured ERP/WMS connectors on an interval."""

    def __init__(
        self,
        *,
        cursors: CursorStore,
        process_record: ProcessRecord,
        tenant_id: str,
        interval_s: float | None = None,
        connector_factory: Callable[[], list[PollingConnector]] | None = None,
    ) -> None:
        self._process_record = process_record
        self._tenant_id = tenant_id
        # Poll cadence is deployment-specific (source-system rate limits vs freshness),
        # so it is configuration, not a code constant. Floor of 5s protects the source
        # systems from an accidental hot loop.
        resolved_interval = (
            _float_env("CONNECTOR_POLL_INTERVAL_SECONDS", 60.0)
            if interval_s is None
            else interval_s
        )
        self._interval_s = max(5.0, resolved_interval)
        self._connector_factory = connector_factory or (
            lambda: build_configured_connectors(cursors=cursors, tenant_id=tenant_id)
        )
        self._task: asyncio.Task | None = None
        self._runs = 0
        self._pulled = 0
        self._last_status = "idle"
        self._last_error: str | None = None

    async def start(self) -> None:
        if not connector_poll_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="shelfwise-connector-poll")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    def status(self) -> dict[str, Any]:
        task = self._task
        running = task is not None and not task.done()
        connectors = self._connector_factory()
        return {
            "enabled": connector_poll_enabled(),
            "running": running,
            "tenant_id": self._tenant_id,
            "interval_s": self._interval_s,
            "configured_systems": sorted(connector.source_system.value for connector in connectors),
            "runs": self._runs,
            "records_pulled": self._pulled,
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_status = "crashed"
                self._last_error = str(exc)[:200]
                _LOG.exception("connector poll run crashed")
            await asyncio.sleep(self._interval_s)

    async def run_once(self) -> int:
        """Pull every configured connector once and ingest yielded records; returns count."""
        connectors = self._connector_factory()
        pulled = 0
        for connector in connectors:
            async for record in connector.pull():
                # process_record does synchronous store I/O (Postgres inserts) - run it off
                # the event loop thread the same way WorkerLoopService does for cascade
                # processing, so one slow connector poll cannot stall every other request.
                await asyncio.to_thread(self._process_record, record)
                pulled += 1
        self._runs += 1
        self._pulled += pulled
        self._last_status = "ok"
        self._last_error = None
        return pulled


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
