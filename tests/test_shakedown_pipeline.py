from __future__ import annotations

import json
from pathlib import Path

from shelfwise.training.dataset import load_training_rows, summarize_rows
from shelfwise.training.shakedown import run_shakedown
from shelfwise.training.simulation import build_shakedown_datasets

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def test_world_simulation_builds_mixed_training_rows(tmp_path) -> None:
    report = build_shakedown_datasets(
        output_dir=tmp_path,
        repo_root=ROOT,
        seed=7,
        train_examples=24,
        eval_examples=8,
    )
    rows = load_training_rows(report["train_path"], repo_root=ROOT, strict=True)
    summary = summarize_rows(rows)

    assert summary["row_count"] == 24
    assert summary["modalities"]["image"] >= 1
    assert summary["modalities"]["audio"] >= 1
    assert summary["modalities"]["video"] >= 1
    assert "simulation_incident" in report["dataset_mixture_breakdown"]
    assert "ambiguous missing evidence" in report["case_breakdown"]


def test_shakedown_dry_run_writes_report_and_eval() -> None:
    run_dir = run_shakedown(
        CONFIG,
        run_name="unit-shakedown",
        dry_run=True,
    )
    report_path = run_dir / "shakedown_report.json"

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "simulation_dataset_generation" in report["stages"]
    assert "dry_run_eval" in report["stages"]
    assert Path(report["dataset"]["train_path"]).exists()
