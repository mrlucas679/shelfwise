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


def test_pending_upsert_preserves_evidence_fields_omitted_by_agentic_update() -> None:
    store = InMemoryDecisionStore()
    store.upsert(
        {
            "id": "dec_shared",
            "status": "pending",
            "summary": "deterministic",
            "expected_outcome": {"stock_at_risk_minor_units": 220_777},
        }
    )

    updated = store.upsert(
        {"id": "dec_shared", "status": "pending", "summary": "agentic verdict"}
    )

    assert updated["summary"] == "agentic verdict"
    assert updated["expected_outcome"] == {"stock_at_risk_minor_units": 220_777}


def test_upsert_cannot_revert_a_decision_a_human_already_resolved() -> None:
    """A cascade re-run (self-heal replay, retried worker delivery) must never win a
    race against a human's approve/reject on the same decision id.

    `upsert()` is the write path every cascade rerun goes through - not just first
    runs. If it always won regardless of status, then a decision a human just rejected
    could be silently reopened back to "pending" (with a fresh action payload) by an
    unrelated retry that shares the same deterministic decision id, and the human's
    own resolution would be the one to disappear.
    """
    store = InMemoryDecisionStore()
    store.upsert(
        {
            "id": "dec_race_terminal",
            "status": "pending",
            "action": {"type": "apply_markdown", "params": {"sku": "SKU-1"}},
        }
    )
    rejected = store.reject("dec_race_terminal")
    assert rejected is not None
    assert rejected["status"] == "rejected"

    replayed = store.upsert(
        {
            "id": "dec_race_terminal",
            "status": "pending",
            "action": {"type": "apply_markdown", "params": {"sku": "SKU-1", "retry": True}},
        }
    )

    assert replayed["status"] == "rejected", (
        "a cascade rerun's upsert reverted a human rejection back to pending"
    )
    assert replayed["action"]["params"].get("retry") is not True, (
        "the rerun's action payload overwrote the resolved decision's recorded action"
    )
    assert store.get("dec_race_terminal")["status"] == "rejected"


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


def test_candidate_store_factory_pairs_history_backend_with_store_backend(monkeypatch) -> None:
    """The candidate store must NEVER mix backends with its history sub-store.

    The 2026-07-15 audit found InMemoryCandidateStore silently persisting candidate
    history to real Postgres because its history default went through an env-sensitive
    factory - and no test could catch it, because every candidate test constructed the
    in-memory class directly, bypassing the factory entirely. This test pins the
    factory wiring itself.
    """
    from shelfwise_backend.candidate_history import InMemoryCandidateHistoryStore
    from shelfwise_backend.candidate_store import InMemoryCandidateStore, create_candidate_store

    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "memory")
    # Poison the Postgres coordinates: if any in-memory path still consults them,
    # construction or first use would fail loudly instead of leaking cross-backend.
    monkeypatch.setenv("DATABASE_URL", "postgresql://poison:poison@127.0.0.1:1/poison")

    store = create_candidate_store()
    assert isinstance(store, InMemoryCandidateStore)
    assert isinstance(store._history, InMemoryCandidateHistoryStore), (
        "an in-memory candidate store delegating history to any other backend is the "
        "exact leak class this test exists to catch"
    )


def test_direct_inmemory_candidate_store_never_touches_env_backends(monkeypatch) -> None:
    """Constructing InMemoryCandidateStore() directly must stay pure in-memory even when
    the ambient environment says postgres - its name is its contract."""
    from shelfwise_backend.candidate_history import InMemoryCandidateHistoryStore
    from shelfwise_backend.candidate_store import InMemoryCandidateStore

    monkeypatch.setenv("SHELFWISE_STORE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://poison:poison@127.0.0.1:1/poison")

    store = InMemoryCandidateStore()
    assert isinstance(store._history, InMemoryCandidateHistoryStore)
