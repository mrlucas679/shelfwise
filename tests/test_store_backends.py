from __future__ import annotations

import pytest

from shelfwise_action import InMemoryDecisionStore, create_decision_store
from shelfwise_backend.chat_store import ChatConversationStore, create_chat_store
from shelfwise_backend.event_bus import InMemoryEventBus, create_event_bus
from shelfwise_backend.event_store import InMemoryEventStore, create_event_store
from shelfwise_backend.worker import InMemoryJournal, create_journal
from shelfwise_connectors import (
    InMemoryInboundRecordStore,
    TaskWriteBackSink,
    create_inbound_record_store,
    create_writeback_sink,
)
from shelfwise_inventory import InMemoryInventoryPositionStore, create_inventory_position_store
from shelfwise_memory import InMemoryLearningStore, create_learning_store
from shelfwise_mlops import (
    InMemoryModelRunRegistry,
    InMemoryPromptRegistry,
    InMemoryTenantFactStore,
    create_model_run_registry,
    create_prompt_registry,
    create_tenant_fact_store,
)
from shelfwise_storage import InMemoryTenantProfileStore, create_tenant_profile_store
from shelfwise_worldgen import InMemoryWorldgenRunStore, create_worldgen_run_store


def test_store_factories_default_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHELFWISE_STORE_BACKEND", raising=False)
    monkeypatch.delenv("SHELFWISE_BUS_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert isinstance(create_decision_store(), InMemoryDecisionStore)
    assert isinstance(create_event_bus(), InMemoryEventBus)
    assert isinstance(create_event_store(), InMemoryEventStore)
    assert isinstance(create_chat_store(), ChatConversationStore)
    assert isinstance(create_journal(), InMemoryJournal)
    assert isinstance(create_learning_store(), InMemoryLearningStore)
    assert isinstance(create_model_run_registry(), InMemoryModelRunRegistry)
    assert isinstance(create_prompt_registry(), InMemoryPromptRegistry)
    assert isinstance(create_tenant_fact_store(), InMemoryTenantFactStore)
    assert isinstance(create_tenant_profile_store(), InMemoryTenantProfileStore)
    assert isinstance(create_writeback_sink(), TaskWriteBackSink)
    assert isinstance(create_inbound_record_store(), InMemoryInboundRecordStore)
    assert isinstance(create_inventory_position_store(), InMemoryInventoryPositionStore)
    assert isinstance(create_worldgen_run_store(), InMemoryWorldgenRunStore)


def test_postgres_store_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_decision_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_event_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_chat_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_journal()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_learning_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_model_run_registry()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_prompt_registry()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_tenant_fact_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_tenant_profile_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_writeback_sink()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_inbound_record_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_inventory_position_store()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_worldgen_run_store()


def test_redis_bus_requires_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_BUS_BACKEND", "redis")
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(ValueError, match="REDIS_URL is required"):
        create_event_bus()


def test_store_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "mongo")

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_decision_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_event_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_chat_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_journal()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_learning_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_model_run_registry()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_prompt_registry()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_tenant_fact_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_tenant_profile_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_writeback_sink()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_inbound_record_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_inventory_position_store()

    with pytest.raises(ValueError, match="unsupported SHELFWISE_STORE_BACKEND"):
        create_worldgen_run_store()


def test_bus_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFWISE_BUS_BACKEND", "rabbit")

    with pytest.raises(ValueError, match="unsupported SHELFWISE_BUS_BACKEND"):
        create_event_bus()
