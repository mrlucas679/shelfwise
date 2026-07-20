from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from shelfwise_backend.connector_poll_service import (
    ConnectorPollService,
    build_configured_connectors,
    connector_poll_enabled,
)
from shelfwise_connectors import (
    InboundRecord,
    InMemoryCursorStore,
    PollingConnector,
    SourceSystem,
    ValidationResult,
)

_ODOO_VARS = {
    "SHELFWISE_CONNECTOR_ODOO_BASE_URL": "https://odoo.example.com",
    "SHELFWISE_CONNECTOR_ODOO_DATABASE": "shelfwise",
    "SHELFWISE_CONNECTOR_ODOO_UID": "7",
    "SHELFWISE_CONNECTOR_ODOO_API_KEY": "secret",
}
_SAP_VARS = {
    "SHELFWISE_CONNECTOR_SAP_BASE_URL": "https://sap.example.com",
    "SHELFWISE_CONNECTOR_SAP_TOKEN": "token",
}
_SYSPRO_VARS = {
    "SHELFWISE_CONNECTOR_SYSPRO_BASE_URL": "https://syspro.example.com",
    "SHELFWISE_CONNECTOR_SYSPRO_TOKEN": "token",
}
_DYNAMICS_VARS = {
    "SHELFWISE_CONNECTOR_DYNAMICS_BASE_URL": "https://bc.example.com/items",
    "SHELFWISE_CONNECTOR_DYNAMICS_TOKEN": "token",
    "SHELFWISE_CONNECTOR_DYNAMICS_LOCATION_ID": "warehouse-1",
}


def _record(object_id: str) -> InboundRecord:
    return InboundRecord(
        tenant_id="sa_retail_demo",
        source_system=SourceSystem.ODOO,
        source_object_type="product_master",
        source_object_id=object_id,
        event_time=datetime(2026, 7, 14, tzinfo=UTC),
        raw_payload={"id": object_id},
        canonical_type="product_master",
        canonical_payload={"sku": object_id},
        correlation_id=object_id,
        validation=ValidationResult(),
    )


class _FakeConnector(PollingConnector):
    source_system = SourceSystem.ODOO

    def __init__(self, cursors: InMemoryCursorStore, *, tenant_id: str, records: list[str]) -> None:
        super().__init__(cursors, tenant_id=tenant_id)
        self._records = records

    async def fetch_page(
        self, cursor: str | None
    ) -> tuple[list[InboundRecord], str | None]:
        if cursor is not None:
            return [], None
        return [_record(object_id) for object_id in self._records], "done"


def test_connector_poll_enabled_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECTOR_POLL_ENABLED", raising=False)
    assert connector_poll_enabled() is False
    monkeypatch.setenv("CONNECTOR_POLL_ENABLED", "true")
    assert connector_poll_enabled() is True


def test_no_connectors_are_built_when_no_credentials_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (*_ODOO_VARS, *_SAP_VARS, *_SYSPRO_VARS, *_DYNAMICS_VARS):
        monkeypatch.delenv(key, raising=False)

    connectors = build_configured_connectors(cursors=InMemoryCursorStore(), tenant_id="t1")

    assert connectors == []


def test_a_system_is_only_polled_when_every_one_of_its_env_vars_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (*_ODOO_VARS, *_SAP_VARS, *_SYSPRO_VARS, *_DYNAMICS_VARS):
        monkeypatch.delenv(key, raising=False)
    # Odoo partially configured (missing api key) must not build a broken connector.
    partial_odoo = dict(_ODOO_VARS)
    del partial_odoo["SHELFWISE_CONNECTOR_ODOO_API_KEY"]
    for key, value in {**partial_odoo, **_SAP_VARS}.items():
        monkeypatch.setenv(key, value)

    connectors = build_configured_connectors(cursors=InMemoryCursorStore(), tenant_id="t1")

    assert [c.source_system for c in connectors] == [SourceSystem.SAP]


def test_all_poll_systems_build_when_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for mapping in (_ODOO_VARS, _SAP_VARS, _SYSPRO_VARS, _DYNAMICS_VARS):
        for key, value in mapping.items():
            monkeypatch.setenv(key, value)

    connectors = build_configured_connectors(cursors=InMemoryCursorStore(), tenant_id="t1")

    assert {c.source_system for c in connectors} == {
        SourceSystem.ODOO,
        SourceSystem.SAP,
        SourceSystem.SYSPRO,
        SourceSystem.DYNAMICS,
    }


def test_run_once_pulls_every_configured_connector_and_ingests_each_record() -> None:
    cursors = InMemoryCursorStore()
    ingested: list[str] = []

    def process_record(record: InboundRecord) -> dict[str, str]:
        ingested.append(record.source_object_id)
        return {"status": "recorded"}

    connectors = [
        _FakeConnector(cursors, tenant_id="sa_retail_demo", records=["p1", "p2"]),
    ]
    service = ConnectorPollService(
        cursors=cursors,
        process_record=process_record,
        tenant_id="sa_retail_demo",
        connector_factory=lambda: connectors,
    )

    pulled = asyncio.run(service.run_once())

    assert pulled == 2
    assert ingested == ["p1", "p2"]
    status = service.status()
    assert status["runs"] == 1
    assert status["records_pulled"] == 2
    assert status["last_status"] == "ok"


def test_run_once_records_the_error_when_a_connector_raises() -> None:
    cursors = InMemoryCursorStore()

    class _BrokenConnector(PollingConnector):
        source_system = SourceSystem.SAP

        def __init__(self) -> None:
            super().__init__(cursors, tenant_id="sa_retail_demo")

        async def fetch_page(
            self, cursor: str | None
        ) -> tuple[list[InboundRecord], str | None]:
            raise RuntimeError("upstream ERP unreachable")

    service = ConnectorPollService(
        cursors=cursors,
        process_record=lambda record: {"status": "recorded"},
        tenant_id="sa_retail_demo",
        connector_factory=lambda: [_BrokenConnector()],
        interval_s=5,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(service.run_once())


def test_service_reports_disabled_when_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECTOR_POLL_ENABLED", raising=False)
    service = ConnectorPollService(
        cursors=InMemoryCursorStore(),
        process_record=lambda record: {"status": "recorded"},
        tenant_id="sa_retail_demo",
        connector_factory=list,
    )

    async def run() -> dict[str, object]:
        await service.start()
        try:
            return service.status()
        finally:
            await service.stop()

    status = asyncio.run(run())

    assert status["enabled"] is False
    assert status["running"] is False
