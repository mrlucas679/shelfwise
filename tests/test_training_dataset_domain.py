from __future__ import annotations

from pathlib import Path

import pytest

from shelfwise.training.dataset import parse_training_row

REPO_ROOT = Path(__file__).resolve().parents[1]


def _raw_row(*, data_domain: object = None) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "row_1",
        "case_type": "general",
        "messages": [
            {"role": "user", "content": "Summarize the shipment discrepancy."},
            {"role": "assistant", "content": "The shipment was short by two cases."},
        ],
        "evidence": [],
        "expected_output": {
            "risk_level": "low",
            "summary": "short summary",
            "findings": [],
            "recommended_actions": [],
            "missing_information": [],
        },
    }
    if data_domain is not None:
        row["data_domain"] = data_domain
    return row


@pytest.mark.parametrize("domain", ["world_simulation", "training_fixture"])
def test_training_row_accepts_training_domains(domain: str) -> None:
    row = parse_training_row(_raw_row(data_domain=domain), repo_root=REPO_ROOT, strict=False)

    assert row.data_domain == domain


def test_training_row_defaults_to_training_fixture_when_domain_omitted() -> None:
    row = parse_training_row(_raw_row(), repo_root=REPO_ROOT, strict=False)

    assert row.data_domain == "training_fixture"


@pytest.mark.parametrize("domain", ["operational_twin", "twin_scenario"])
def test_training_row_rejects_live_twin_domains(domain: str) -> None:
    """Operational twin state must never enter a training dataset, per the twin/training
    boundary: only world_simulation or reviewed training_fixture rows may be used."""
    with pytest.raises(ValueError, match="training dataset cannot consume"):
        parse_training_row(_raw_row(data_domain=domain), repo_root=REPO_ROOT, strict=False)


def test_training_row_rejects_unknown_domain() -> None:
    with pytest.raises(ValueError, match="unsupported data_domain"):
        parse_training_row(
            _raw_row(data_domain="not_a_real_domain"),
            repo_root=REPO_ROOT,
            strict=False,
        )
