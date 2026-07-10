from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_APP = ROOT / "frontend" / "src" / "App.tsx"


def _source() -> str:
    return FRONTEND_APP.read_text(encoding="utf-8")


def _type_union(source: str, type_name: str) -> set[str]:
    multiline = re.search(rf"type {type_name} =\n((?:  \| '[^']+'\n?)+)", source)
    if multiline is not None:
        return set(re.findall(r"'([^']+)'", multiline.group(1)))
    single_line = re.search(rf"type {type_name} = ([^\n]+)", source)
    assert single_line is not None, f"{type_name} union not found"
    return set(re.findall(r"'([^']+)'", single_line.group(1)))


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_sidebar_page_stack_stays_utility_only() -> None:
    source = _source()

    assert _type_union(source, "SidebarPage") == {"settings"}
    assert _type_union(source, "WorkspaceSurface") >= {
        "products",
        "to-order",
        "sell-first",
        "deliveries",
        "cold-chain",
        "connections",
        "operations",
        "results",
    }


def test_sidebar_is_attention_entrypoint_not_catalogue_browser() -> None:
    source = _source()
    sidebar = _between(source, "function Sidebar(", "type WorkspaceRowProps")

    assert "This list is attention-only" in sidebar
    assert "ops.productAttention.items" in sidebar
    assert "openWorkspace('products'" in sidebar
    assert "Search catalogue for" in sidebar
    assert "onOpenWorkspace(surface, options)" in sidebar
    assert "/products/search" not in sidebar


def test_products_workspace_owns_bounded_catalogue_search() -> None:
    source = _source()
    workspace = _between(source, "function WorkspaceScreen(", "function App()")

    assert "params.set('limit', '20')" in workspace
    assert "fetchOptional<ProductSearchPayload>(`/products/search?" in workspace
    assert "Catalogue search" in workspace
    assert "inventory never moves into the sidebar" in workspace


def test_product_workspace_drills_into_product_card_and_lots() -> None:
    source = _source()
    workspace = _between(source, "function WorkspaceScreen(", "function App()")

    assert "selectedProductKey" in workspace
    assert "setSelectedProductKey" in workspace
    assert "fefo_batches" in source
    assert 'WorkspaceSection title="Product card"' in workspace
    assert 'WorkspaceSection title="Lot rotation"' in workspace
    assert 'aria-label="Selected product lots"' in workspace


def test_product_workspace_explains_bounded_search_receipt() -> None:
    source = _source()
    workspace = _between(source, "function WorkspaceScreen(", "function App()")

    assert "catalogSearchReceipt" in workspace
    assert "synthetic_scan_budget" in workspace
    assert 'WorkspaceSection title="Search receipt"' in workspace
    assert "Bounded catalogue scan" in workspace
    assert "Attention products are ranked first" in workspace


def test_settings_uses_loaded_tenant_profile_not_placeholder_copy() -> None:
    source = _source()
    sidebar = _between(source, "function Sidebar(", "type WorkspaceRowProps")
    app_load = _between(source, "function App()", "const conn =")

    assert "fetchIfAvailable<{ profile?: JsonObject }>('/tenants/me')" in app_load
    assert "ops.tenantProfile" in sidebar
    assert "Connector policy" in sidebar
    assert "Company account sign-in is coming soon" not in sidebar


def test_connection_workspace_shows_methods_for_gated_endpoint_rows() -> None:
    source = _source()
    workspace = _between(source, "function WorkspaceScreen(", "function App()")

    assert "Webhook, scan, and intelligence gates" in workspace
    assert "meta={`${item.method} ${item.path}`}" in workspace
