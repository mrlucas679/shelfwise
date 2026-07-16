from __future__ import annotations

import pytest

from shelfwise_backend.context_assembler import assemble_context


def test_context_assembler_keeps_evidence_and_emits_manifest() -> None:
    bundle = assemble_context(
        {
            "tenant_id": "tenant-a",
            "tool_results": {"stock": {"on_hand": 12}},
            "decisions": [{"id": "dec-1", "status": "pending"}],
            "source_refs": ["wms:stock:1"],
            "large_history": list(range(100)),
        },
        decision_type="inventory",
    )

    assert bundle.payload["tool_results"]["stock"]["on_hand"] == 12
    assert bundle.source_refs == ("wms:stock:1",)
    assert bundle.missing_data == ()
    assert bundle.evidence_score == 1.0
    assert bundle.manifest["decision_type"] == "inventory"
    assert bundle.token_estimate > 0


def test_context_assembler_reports_thin_evidence_and_bounds_payload() -> None:
    bundle = assemble_context(
        {"decisions": [], "large": "x" * 100_000},
        decision_type="expiry",
        max_chars=2_000,
    )

    assert bundle.missing_data == ("tool_results", "decisions")
    assert bundle.evidence_score == 0.0
    assert bundle.manifest["context_chars"] <= 2_000


def test_context_assembler_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="decision_type"):
        assemble_context({}, decision_type=" ")
    with pytest.raises(ValueError, match="max_chars"):
        assemble_context({}, decision_type="chat", max_chars=25_000)


def test_context_assembler_hard_caps_priority_fallback() -> None:
    bundle = assemble_context(
        {
            "subject": "x" * 10_000,
            "tool_results": {
                "stock": [
                    {"sku": str(index), "value": "y" * 500} for index in range(500)
                ]
            },
        },
        decision_type="inventory",
        max_chars=100,
    )

    assert bundle.manifest["context_chars"] <= 100
    assert bundle.manifest["truncated"] is True
