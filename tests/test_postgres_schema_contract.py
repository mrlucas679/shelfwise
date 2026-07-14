"""Production-schema smoke test for every Postgres-backed application store."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from shelfwise_action.store import PostgresDecisionStore
from shelfwise_backend.candidate_store import PostgresCandidateStore
from shelfwise_backend.chat_store import PostgresChatConversationStore
from shelfwise_backend.event_store import PostgresEventStore
from shelfwise_backend.open_orders import PostgresOpenOrderStore
from shelfwise_backend.worker.journal import PostgresJournal
from shelfwise_catalog.store import PostgresProductCatalogStore
from shelfwise_connectors.inbound_store import PostgresInboundRecordStore
from shelfwise_connectors.writeback import PostgresTaskWriteBackSink
from shelfwise_inventory.store import PostgresInventoryPositionStore
from shelfwise_memory import PostgresLearningStore
from shelfwise_mlops import (
    PostgresModelRunRegistry,
    PostgresPromptRegistry,
    PostgresTenantFactStore,
)
from shelfwise_storage import bind_tenant_context, reset_tenant_context
from shelfwise_storage.tenant_profiles import PostgresTenantProfileStore
from shelfwise_twin import (
    PostgresCalibrationRegistry,
    PostgresOnboardingManifestRegistry,
    PostgresScenarioBranchStore,
    PostgresTwinStore,
)
from shelfwise_worldgen.store import PostgresWorldgenRunStore
from shelfwise_worldgen.world_store import PostgresWorldSnapshotStore

_DATABASE_URL = os.getenv("SHELFWISE_TEST_DATABASE_URL", "")
_TENANT_ID = "postgres_schema_contract"

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="SHELFWISE_TEST_DATABASE_URL not set - production schema smoke test skipped",
)


def test_central_schema_supports_every_postgres_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch query/schema drift before a production-only endpoint discovers it."""
    monkeypatch.setenv("SHELFWISE_AUTO_SCHEMA", "false")
    token = bind_tenant_context(_TENANT_ID)
    try:
        assert PostgresDecisionStore(_DATABASE_URL).list() == []
        assert PostgresCandidateStore(_DATABASE_URL).list(_TENANT_ID) == []
        assert PostgresChatConversationStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            user_id="schema-user",
        ) == []
        assert PostgresEventStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            data_domain="operational_twin",
        ) == []
        assert PostgresOpenOrderStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            data_domain="operational_twin",
        ) == []
        assert PostgresJournal(_DATABASE_URL).list_runs(
            tenant_id=_TENANT_ID,
            data_domain="operational_twin",
        ) == []
        assert PostgresProductCatalogStore(_DATABASE_URL).list_products(
            tenant_id=_TENANT_ID
        ) == []
        assert PostgresInboundRecordStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID
        ) == []
        assert PostgresTaskWriteBackSink(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            data_domain="operational_twin",
        ) == []
        assert PostgresInventoryPositionStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID
        ) == []
        assert PostgresLearningStore(_DATABASE_URL).thresholds(
            tenant_id=_TENANT_ID,
            data_domain="world_simulation",
        ) == {}
        assert PostgresLearningStore(_DATABASE_URL).list_events(
            tenant_id=_TENANT_ID,
            data_domain="world_simulation",
        ) == []
        assert PostgresModelRunRegistry(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            data_domain="world_simulation",
        ) == []
        assert PostgresPromptRegistry(_DATABASE_URL).list(tenant_id=_TENANT_ID) == []
        assert PostgresTenantFactStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID,
            data_domain="world_simulation",
        ) == []
        assert PostgresTenantProfileStore(_DATABASE_URL).get(_TENANT_ID) is None
        assert PostgresWorldgenRunStore(_DATABASE_URL).list(
            tenant_id=_TENANT_ID
        ) == []
        assert PostgresWorldSnapshotStore(_DATABASE_URL).get(_TENANT_ID) is None

        twin = PostgresTwinStore(_DATABASE_URL)
        assert twin.list_entities(_TENANT_ID) == []
        assert twin.list_relationships(_TENANT_ID) == []
        assert twin.list_observations(_TENANT_ID) == []
        assert twin.list_properties(_TENANT_ID) == []
        assert PostgresCalibrationRegistry(_DATABASE_URL).list(
            _TENANT_ID, "schema-store"
        ) == []
        assert PostgresScenarioBranchStore(_DATABASE_URL).get(
            _TENANT_ID, "schema-store", "schema-branch"
        ) is None
        assert PostgresOnboardingManifestRegistry(_DATABASE_URL).get(
            _TENANT_ID, "schema-store"
        ) is None
    finally:
        reset_tenant_context(token)


def test_postgres_chat_lock_preserves_concurrent_messages_and_user_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the cross-replica advisory lock against a real Postgres server."""
    monkeypatch.setenv("SHELFWISE_AUTO_SCHEMA", "false")
    store = PostgresChatConversationStore(_DATABASE_URL, history_limit=100)
    tenant_id = "postgres_chat_concurrency"
    user_id = "user-a"
    conversation_id = "shared-conversation"
    # This test writes a fixed tenant/conversation with no per-test isolation (unlike
    # test_central_schema_supports_every_postgres_store's never-before-used tenant IDs) - a
    # prior run against this same persistent database would leave messages behind and this run
    # would silently append more, false-failing the len(...) == 32 assertion below. `clear()`
    # deletes only under the ambient ("ContextVar default") tenant per RLS, so it must run
    # under this test's own tenant context, not whatever tenant happens to be ambient.
    token = bind_tenant_context(tenant_id)
    try:
        store.clear()
    finally:
        reset_tenant_context(token)

    def append(index: int) -> None:
        token = bind_tenant_context(tenant_id)
        try:
            with store.locked(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            ):
                store.append_exchange(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=f"message-{index}",
                    question=f"Question {index}",
                    answer=f"Answer {index}",
                    metadata={"data_domain": "operational_twin"},
                )
        finally:
            reset_tenant_context(token)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(16)))

    conversation = store.get(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    assert conversation is not None
    assert len(conversation["messages"]) == 32
    assert {item["id"] for item in conversation["messages"] if item["role"] == "user"} == {
        f"message-{index}" for index in range(16)
    }
    assert store.get(
        tenant_id=tenant_id,
        user_id="user-b",
        conversation_id=conversation_id,
    ) is None
    assert store.get(
        tenant_id="postgres_chat_other_tenant",
        user_id=user_id,
        conversation_id=conversation_id,
    ) is None
