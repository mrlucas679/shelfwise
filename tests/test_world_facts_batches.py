from __future__ import annotations

from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_worldgen.populate import DEMO_POLICY, populate_world
from shelfwise_worldgen.world_store import InMemoryWorldSnapshotStore


def test_generated_perishable_hero_keeps_multiple_lots_and_fefo_reconciles() -> None:
    """The generated-world source preserves lot facts instead of collapsing them to a SKU."""
    store = InMemoryWorldSnapshotStore()
    tenant_id = "batch-world-test"
    populate_world(DEMO_POLICY, tenant_id=tenant_id, store=store)
    facts = WorldFactsProvider(store)

    snapshot = store.get(tenant_id)
    assert snapshot is not None
    stock = next(row for row in snapshot["payload"]["stock"] if len(row["batches"]) >= 2)
    snapshot["payload"]["constraints"]["hero_sku"] = stock["sku"]
    store.save(snapshot)
    assert len(stock["batches"]) >= 2
    assert sum(batch["on_hand"] for batch in stock["batches"]) == stock["on_hand"]

    split = facts.get_store_intelligence(tenant_id)["batch_split"]
    assert len(split["fefo_batches"]) == len(stock["batches"])
    assert split["total_units"] == stock["on_hand"]
    assert split["fefo_batches"] == sorted(
        split["fefo_batches"],
        key=lambda batch: (batch["expiry_date"], batch["received_date"], batch["lot"]),
    )


def test_legacy_world_snapshot_without_batches_remains_readable() -> None:
    """Snapshots created before lot tracking use the aggregate row as one legacy lot."""
    store = InMemoryWorldSnapshotStore()
    store.save(
        {
            "tenant_id": "legacy-batch-world-test",
            "seed": 1,
            "policy": "legacy",
            "generated_at": "2026-07-06T08:00:00+00:00",
            "payload": {
                "products": [
                    {
                        "sku": "P00000001",
                        "name": "Legacy milk",
                        "category": "Dairy",
                        "supplier": "Legacy Supplier",
                        "unit_cost": 10,
                        "unit_price": 20,
                    }
                ],
                "stock": [
                    {
                        "sku": "P00000001",
                        "location": "store_01",
                        "on_hand": 8,
                        "reorder_point": 4,
                        "expiry_date": "2026-07-08",
                        "received_date": "2026-07-05",
                    }
                ],
                "sales": [],
                "suppliers": [
                    {
                        "supplier_id": "supplier:legacy",
                        "name": "Legacy Supplier",
                        "lead_time_days": 2,
                        "recent_delay": False,
                        "distance_km": 20,
                        "available_units": 100,
                    }
                ],
                "sites": [],
                "constraints": {"hero_sku": "P00000001", "low_stock_skus": []},
            },
        }
    )

    intelligence = WorldFactsProvider(store).get_store_intelligence("legacy-batch-world-test")
    split = intelligence["batch_split"]
    assert split["total_units"] == 8
    assert len(split["fefo_batches"]) == 1
    assert split["fefo_batches"][0]["lot"] == "LOT-P00000001"
