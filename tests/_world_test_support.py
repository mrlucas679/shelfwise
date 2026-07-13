"""Shared helper so tests reference a real generated SKU instead of a hardcoded literal.

The old CSV seed guaranteed SKU "4011" always existed; the generated world assigns
algorithmic product IDs instead, so any test driving a real cascade/tool call must resolve
a genuine SKU from the same default facts provider the app uses, rather than hardcoding one.
"""

from __future__ import annotations

from shelfwise_backend.world_facts import WorldFactsProvider
from shelfwise_worldgen.world_store import InMemoryWorldSnapshotStore

DEFAULT_TEST_TENANT = "sa_retail_demo"


def demo_facts() -> WorldFactsProvider:
    return WorldFactsProvider(InMemoryWorldSnapshotStore())


def demo_sku(tenant_id: str = DEFAULT_TEST_TENANT) -> str:
    return demo_facts().get_hero_sku(tenant_id)
