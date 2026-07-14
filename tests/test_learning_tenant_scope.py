from __future__ import annotations

from shelfwise_memory import InMemoryLearningStore


def _approved_decision(decision_id: str, tenant_id: str, predicted_units: int) -> dict[str, object]:
    return {
        "id": decision_id,
        "status": "approved",
        "tenant_id": tenant_id,
        "action": {"type": "apply_markdown", "params": {"sku": "sku-1"}},
        "expected_outcome": {
            "predicted_sell_through_units": predicted_units,
            "predicted_waste_units": 2,
            "markdown_margin_minor_units": 100,
            "incremental_profit_minor_units": 500,
        },
    }


def test_in_memory_learning_store_keeps_tenant_identity_and_scopes_reads() -> None:
    store = InMemoryLearningStore()

    tenant_a_event = store.record_approved_decision(
        _approved_decision("shared-decision", "tenant_a", 10)
    )
    tenant_b_event = store.record_approved_decision(
        _approved_decision("shared-decision", "tenant_b", 20)
    )

    assert tenant_a_event["tenant_id"] == "tenant_a"
    assert tenant_b_event["tenant_id"] == "tenant_b"
    assert store.list_events(tenant_id="tenant_a") == [tenant_a_event]
    assert store.list_events(tenant_id="tenant_b") == [tenant_b_event]
    assert store.thresholds(tenant_id="tenant_a") != store.thresholds(tenant_id="tenant_b")
