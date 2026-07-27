"""Background polling for ERP/WMS connectors that use pull, not webhook, transport.

Wires the already-built `PollingConnector.pull()` cursor/dedup machinery to the same
ingestion pipeline every webhook/CSV/manual intake route already uses (injected as
`process_record`, so this module has no dependency on `app.py`), on an interval, for the
single tenant this deployment is configured for.

Per-tenant *credential storage* is real now - see `shelfwise_connectors.credentials`
(encrypted, RLS-scoped). What remains a deliberate, explicit non-goal here is turning this
single background loop into a per-tenant polling loop: this deployment still polls one
tenant's connectors per process (`SHELFWISE_TENANT_ID`), and `resolve_connector_credentials`
lets that one tenant's stored credentials take priority over the env-var fallback - a real
step toward multi-tenancy, not the full architecture change (N tenants, N poll schedules,
per-tenant backpressure/failure isolation) that a truly concurrent multi-tenant poll loop
would need. A system is polled only when its resolved credentials are completely set; a
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
    SourceSystem,
    SysproInventoryConnector,
    resolve_connector_credentials,
)

_LOG = logging.getLogger("shelfwise.connector_poll")

ProcessRecord = Callable[[Any], dict[str, Any]]


def connector_poll_enabled() -> bool:
    return os.getenv("CONNECTOR_POLL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _resolved_fields(
    *,
    credential_store: Any,
    tenant_id: str,
    system: SourceSystem,
    env_fallback: dict[str, str],
) -> dict[str, str]:
    """Resolve one system's credential fields, tenant-stored first, env-var fallback second.

    A field present in `env_fallback` with an empty value is dropped before resolution, so
    a tenant's stored credentials are used in full even when the env-var fallback is only
    partially configured (or not configured at all) for that field.
    """
    non_blank_fallback = {key: value for key, value in env_fallback.items() if value}
    if credential_store is None:
        return non_blank_fallback
    return resolve_connector_credentials(
        credential_store,
        tenant_id=tenant_id,
        system=system,
        env_fallback=non_blank_fallback,
    )


def build_configured_connectors(
    *,
    cursors: CursorStore,
    tenant_id: str,
    credential_store: Any = None,
) -> list[PollingConnector]:
    """Construct one connector per polling system with complete resolved credentials.

    `credential_store` is optional and defaults to `None` (env-vars only, the original
    single-tenant behavior, unchanged) - passing a real store lets this tenant's own stored
    credentials take priority over the shared env-var defaults, per system.
    """
    connectors: list[PollingConnector] = []

    odoo = _resolved_fields(
        credential_store=credential_store,
        tenant_id=tenant_id,
        system=SourceSystem.ODOO,
        env_fallback={
            "base_url": os.getenv("SHELFWISE_CONNECTOR_ODOO_BASE_URL", "").strip(),
            "database": os.getenv("SHELFWISE_CONNECTOR_ODOO_DATABASE", "").strip(),
            "uid": os.getenv("SHELFWISE_CONNECTOR_ODOO_UID", "").strip(),
            "api_key": os.getenv("SHELFWISE_CONNECTOR_ODOO_API_KEY", "").strip(),
        },
    )
    if {"base_url", "database", "uid", "api_key"} <= odoo.keys():
        try:
            connectors.append(
                OdooProductConnector(
                    cursors,
                    base_url=odoo["base_url"],
                    database=odoo["database"],
                    uid=int(odoo["uid"]),
                    api_key=odoo["api_key"],
                    tenant_id=tenant_id,
                )
            )
        except ValueError:
            _LOG.warning("Odoo credential 'uid' is not an integer - Odoo poll disabled")

    sap = _resolved_fields(
        credential_store=credential_store,
        tenant_id=tenant_id,
        system=SourceSystem.SAP,
        env_fallback={
            "base_url": os.getenv("SHELFWISE_CONNECTOR_SAP_BASE_URL", "").strip(),
            "token": os.getenv("SHELFWISE_CONNECTOR_SAP_TOKEN", "").strip(),
        },
    )
    if {"base_url", "token"} <= sap.keys():
        connectors.append(
            SapS4InventoryConnector(
                cursors, base_url=sap["base_url"], token=sap["token"], tenant_id=tenant_id
            )
        )

    syspro = _resolved_fields(
        credential_store=credential_store,
        tenant_id=tenant_id,
        system=SourceSystem.SYSPRO,
        env_fallback={
            "base_url": os.getenv("SHELFWISE_CONNECTOR_SYSPRO_BASE_URL", "").strip(),
            "token": os.getenv("SHELFWISE_CONNECTOR_SYSPRO_TOKEN", "").strip(),
        },
    )
    if {"base_url", "token"} <= syspro.keys():
        connectors.append(
            SysproInventoryConnector(
                cursors,
                base_url=syspro["base_url"],
                token=syspro["token"],
                tenant_id=tenant_id,
            )
        )

    dynamics = _resolved_fields(
        credential_store=credential_store,
        tenant_id=tenant_id,
        system=SourceSystem.DYNAMICS,
        env_fallback={
            "base_url": os.getenv("SHELFWISE_CONNECTOR_DYNAMICS_BASE_URL", "").strip(),
            "token": os.getenv("SHELFWISE_CONNECTOR_DYNAMICS_TOKEN", "").strip(),
            "location_id": os.getenv(
                "SHELFWISE_CONNECTOR_DYNAMICS_LOCATION_ID", ""
            ).strip(),
        },
    )
    if {"base_url", "token", "location_id"} <= dynamics.keys():
        connectors.append(
            DynamicsBusinessCentralInventoryConnector(
                cursors,
                base_url=dynamics["base_url"],
                token=dynamics["token"],
                location_id=dynamics["location_id"],
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
        credential_store: Any = None,
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
            lambda: build_configured_connectors(
                cursors=cursors, tenant_id=tenant_id, credential_store=credential_store
            )
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
            except Exception:
                self._last_status = "crashed"
                # Connector status is a public diagnostic route. Do not return an
                # upstream URL, credential-adjacent message, or implementation detail.
                self._last_error = "poll_failed"
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
