"""Age-based simulation-history retention ("Things that needs to be implemented" item 7)."""

from __future__ import annotations

import asyncio
import os

import pytest

from shelfwise_backend.retention import (
    RetentionService,
    prune_simulation_history,
    retention_days,
    retention_enabled,
)

_DATABASE_URL = os.getenv("SHELFWISE_TEST_DATABASE_URL", "")


def test_retention_is_off_by_default_and_age_floor_cannot_be_undercut(monkeypatch) -> None:
    monkeypatch.delenv("RETENTION_ENABLED", raising=False)
    assert retention_enabled() is False, "retention must never run unless explicitly enabled"
    monkeypatch.setenv("RETENTION_DAYS", "1")
    assert retention_days() == 7.0, (
        "a mis-set env var must not be able to eat last week's evidence - 7-day floor"
    )
    monkeypatch.setenv("RETENTION_DAYS", "45")
    assert retention_days() == 45.0


def test_retention_refuses_the_memory_backend_with_an_honest_reason(monkeypatch) -> None:
    monkeypatch.setenv("RETENTION_ENABLED", "true")
    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "memory")
    service = RetentionService()
    asyncio.run(service.start())
    status = service.status()
    assert status["running"] is False
    assert "in-memory backend" in (status["refused_reason"] or "")
    assert status["domain"] == "world_simulation"


@pytest.mark.skipif(not _DATABASE_URL, reason="SHELFWISE_TEST_DATABASE_URL not set")
def test_prune_deletes_only_aged_simulation_rows_never_operational_or_pending(
    monkeypatch,
) -> None:
    """The scope contract, proven against real Postgres: aged world_simulation events and
    RESOLVED decisions go; operational rows and pending decisions survive any age."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from shelfwise_storage import bind_tenant_context, connect, reset_tenant_context

    monkeypatch.setenv("RETENTION_DAYS", "30")
    tenant = f"retention_probe_{uuid4().hex[:10]}"
    old = datetime.now(UTC) - timedelta(days=90)
    token = bind_tenant_context(tenant)
    try:
        with connect(_DATABASE_URL, tenant_id=tenant) as conn:
            for domain, event_id in (
                ("world_simulation", "evt_old_sim"),
                ("operational_twin", "evt_old_ops"),
            ):
                conn.execute(
                    """insert into shelfwise_events
                       (id, tenant_id, data_domain, event_type, event_ts, payload,
                        received_at, published)
                       values (%s, %s, %s, 'scan', %s, '{}'::jsonb, %s, true)""",
                    (event_id, tenant, domain, old, old),
                )
            for status_value, dec_id in (("approved", "dec_old_done"), ("pending", "dec_old_open")):
                conn.execute(
                    """insert into shelfwise_decisions
                       (id, tenant_id, data_domain, status, payload, created_at, updated_at)
                       values (%s, %s, 'world_simulation', %s, '{}'::jsonb, %s, %s)""",
                    (f"{dec_id}_{tenant}", tenant, status_value, old, old),
                )
            conn.commit()

        counts = prune_simulation_history(database_url=_DATABASE_URL)
        assert counts["events"] >= 1
        assert counts["resolved_decisions"] >= 1

        with connect(_DATABASE_URL, tenant_id=tenant) as conn:
            sim = conn.execute(
                "select count(*) from shelfwise_events where tenant_id=%s and id='evt_old_sim'",
                (tenant,),
            ).fetchone()["count"]
            ops = conn.execute(
                "select count(*) from shelfwise_events where tenant_id=%s and id='evt_old_ops'",
                (tenant,),
            ).fetchone()["count"]
            pending = conn.execute(
                "select count(*) from shelfwise_decisions where tenant_id=%s and status='pending'",
                (tenant,),
            ).fetchone()["count"]
        assert sim == 0, "aged simulation event must be pruned"
        assert ops == 1, "operational rows are an audit trail and must NEVER be pruned"
        assert pending == 1, "a pending decision is live work at any age"
    finally:
        reset_tenant_context(token)
