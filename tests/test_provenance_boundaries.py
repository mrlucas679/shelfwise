from __future__ import annotations

import json
from pathlib import Path

import pytest

from shelfwise.training.dataset import load_training_rows
from shelfwise.training.simulation import build_shakedown_datasets
from shelfwise_twin import InMemoryTwinStore, TwinService

ROOT = Path(__file__).resolve().parents[1]


def test_world_simulation_dataset_is_explicitly_labeled(tmp_path: Path) -> None:
    report = build_shakedown_datasets(
        output_dir=tmp_path,
        repo_root=ROOT,
        seed=17,
        train_examples=4,
        eval_examples=2,
    )

    rows = load_training_rows(report["train_path"], repo_root=ROOT)

    assert rows
    assert {row.data_domain for row in rows} == {"world_simulation"}
    assert report["train_summary"]["data_domains"] == {"world_simulation": 4}


def test_training_harness_does_not_write_to_twin_store(tmp_path: Path) -> None:
    twin = TwinService(InMemoryTwinStore())

    build_shakedown_datasets(
        output_dir=tmp_path,
        repo_root=ROOT,
        seed=21,
        train_examples=3,
        eval_examples=1,
    )

    assert twin.store.list_entities("sa_retail_demo") == []


def test_training_loader_rejects_operational_twin_data(tmp_path: Path) -> None:
    row = {
        "id": "live-leak",
        "data_domain": "operational_twin",
        "case_type": "general",
        "messages": [{"role": "user", "content": "Answer from the shop."}],
        "evidence": [],
        "expected_output": {
            "summary": "No training example.",
            "risk_level": "low",
            "findings": [],
            "recommended_actions": [],
            "missing_information": [],
        },
    }
    path = tmp_path / "live-leak.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot consume"):
        load_training_rows(path, repo_root=ROOT)
