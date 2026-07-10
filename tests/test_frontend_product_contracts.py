from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_TSX = ROOT / "frontend" / "src" / "App.tsx"


def test_frontend_connects_product_attention_and_search_endpoints() -> None:
    source = APP_TSX.read_text(encoding="utf-8")

    assert "/products/attention?limit=20" in source
    assert "fetchOptional<ProductSearchPayload>(`/products/search?" in source
    assert "ProductAttentionPayload" in source
    assert "ProductSearchPayload" in source


def test_frontend_exposes_runtime_config_and_inference_badge() -> None:
    source = APP_TSX.read_text(encoding="utf-8")
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    runtime_config = (ROOT / "frontend" / "public" / "shelfwise-config.js").read_text(
        encoding="utf-8"
    )

    assert "window.SHELFWISE_CONFIG" in source
    assert "runtimeConfig()?.apiBase" in source
    assert "InferencePill" in source
    assert "AMD vLLM" in source
    assert "/shelfwise-config.js" in index
    assert "apiBase" in runtime_config


def test_search_is_attention_first_not_inventory_dump() -> None:
    source = APP_TSX.read_text(encoding="utf-8")

    assert "This list is attention-only" in source
    assert "Bounded catalogue scan" in source
    assert "Scan window" in source
    assert "Source mix" in source
    assert "catalogResults" in source


def test_product_card_keeps_lots_one_level_deeper() -> None:
    source = APP_TSX.read_text(encoding="utf-8")

    assert "selectedProductKey" in source
    assert "selectedProductBatches" in source
    assert "fefo_batches" in source
    assert 'aria-label="Selected product lots"' in source
