"""Real Postgres integration test for the generated-world store.

Every other test in this suite exercises the Postgres store classes' SQL through mocks or
the in-memory backend only - never a live connection. This is the one test that actually
opens a real Postgres connection, so a schema typo, RLS policy mismatch, or broken query
would be caught in CI instead of surfacing for the first time in production.

Skipped unless SHELFWISE_TEST_DATABASE_URL is set to a real Postgres connection string for
a database with schema.sql + init_app_role.sh already applied (see DEMO_RUNBOOK.md for the
docker run invocation used to stand this up locally).
"""

from __future__ import annotations

import os

import pytest

from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_worldgen.populate import DEMO_POLICY, populate_world
from shelfwise_worldgen.world_store import PostgresWorldSnapshotStore

_DATABASE_URL = os.getenv("SHELFWISE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="SHELFWISE_TEST_DATABASE_URL not set - real Postgres integration test skipped",
)


@pytest.fixture
def pg_store(monkeypatch: pytest.MonkeyPatch) -> PostgresWorldSnapshotStore:
    # The connecting role is intentionally restricted (no CREATE) - schema.sql +
    # init_app_role.sh already applied it. Force auto-schema off so this test only needs
    # SHELFWISE_TEST_DATABASE_URL set, not a second env var to avoid a confusing
    # InsufficientPrivilege error on an unrelated schema-bootstrap attempt.
    monkeypatch.setenv("SHELFWISE_AUTO_SCHEMA", "false")
    store = PostgresWorldSnapshotStore(_DATABASE_URL)
    store.clear()
    yield store
    store.clear()


def test_populate_world_round_trips_through_real_postgres(
    pg_store: PostgresWorldSnapshotStore,
) -> None:
    tenant_id = "pg_integration_test_tenant"

    receipt = populate_world(DEMO_POLICY, tenant_id=tenant_id, store=pg_store)

    assert receipt.product_count == DEMO_POLICY.assortment_size
    assert receipt.hero_sku

    snapshot = pg_store.get(tenant_id)
    assert snapshot is not None
    assert snapshot["tenant_id"] == tenant_id
    assert snapshot["seed"] == DEMO_POLICY.seed
    assert len(snapshot["payload"]["products"]) == DEMO_POLICY.assortment_size
    assert len(snapshot["payload"]["stock"]) == DEMO_POLICY.assortment_size
    assert snapshot["payload"]["constraints"]["hero_sku"] == receipt.hero_sku


def test_world_facts_provider_reads_from_real_postgres(
    pg_store: PostgresWorldSnapshotStore,
) -> None:
    tenant_id = "pg_integration_test_tenant_2"
    populate_world(DEMO_POLICY, tenant_id=tenant_id, store=pg_store)
    facts = WorldFactsProvider(pg_store)

    hero_sku = facts.get_hero_sku(tenant_id)
    scenario = facts.get_scenario_facts(tenant_id, hero_sku)
    intelligence = facts.get_store_intelligence(tenant_id)
    candidates = facts.get_sourcing_candidates(tenant_id, hero_sku)

    assert scenario.sku == hero_sku
    assert scenario.units_on_hand >= 0
    assert intelligence["batch_split"]["sku"] == hero_sku
    assert intelligence["supplier_cover"] is not None
    assert len(candidates) > 0


def test_world_snapshot_is_tenant_isolated_in_postgres(
    pg_store: PostgresWorldSnapshotStore,
) -> None:
    """Two tenants populated with the same seed must not see each other's snapshot row."""
    tenant_a = "pg_integration_tenant_a"
    tenant_b = "pg_integration_tenant_b"
    populate_world(DEMO_POLICY, tenant_id=tenant_a, store=pg_store)
    populate_world(DEMO_POLICY, tenant_id=tenant_b, store=pg_store)

    snap_a = pg_store.get(tenant_a)
    snap_b = pg_store.get(tenant_b)

    assert snap_a is not None and snap_a["tenant_id"] == tenant_a
    assert snap_b is not None and snap_b["tenant_id"] == tenant_b
    assert pg_store.get("pg_integration_tenant_nonexistent") is None
