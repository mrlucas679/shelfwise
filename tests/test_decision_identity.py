from __future__ import annotations

from datetime import UTC, datetime

from shelfwise_backend.cascade import run_golden_cascade, run_sales_cascade
from shelfwise_contracts import Event, EventSource, EventType


def _sale(event_id: str, *, tenant_id: str = "sa_retail_demo") -> Event:
    return Event(
        id=event_id,
        type=EventType.SALE,
        ts=datetime(2026, 7, 10, 9, tzinfo=UTC),
        actor="store_obs_main",
        payload={"sku": "4011", "quantity": 3, "unit_price": "38.99"},
        source=EventSource.POS_CSV,
        tenant_id=tenant_id,
        correlation_id="world_shared_run",
    )


def test_manual_scenario_runs_mint_distinct_decisions() -> None:
    first = run_golden_cascade()
    second = run_golden_cascade()

    assert first["scenario"] == second["scenario"]
    assert first["decision"]["scenario_id"] == first["scenario"]
    assert first["decision"]["id"] != second["decision"]["id"]


def test_event_decision_identity_is_replay_safe_and_not_correlation_scoped() -> None:
    first_event = _sale("evt_sale_001")
    second_event = _sale("evt_sale_002")

    first = run_sales_cascade(first_event)["decision"]
    replay = run_sales_cascade(first_event)["decision"]
    second = run_sales_cascade(second_event)["decision"]

    assert first["id"] == replay["id"]
    assert first["id"] != second["id"]
    assert first["caused_by"] == [first_event.id]
    assert second["caused_by"] == [second_event.id]


def test_event_decision_identity_includes_tenant_scope() -> None:
    first = run_sales_cascade(_sale("shared-source-id", tenant_id="tenant-a"))["decision"]
    second = run_sales_cascade(_sale("shared-source-id", tenant_id="tenant-b"))["decision"]

    assert first["id"] != second["id"]
