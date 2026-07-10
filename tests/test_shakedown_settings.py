from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shelfwise.training.config import load_training_config
from shelfwise.training.shakedown import run_shakedown
from shelfwise.training.simulation import build_shakedown_datasets

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def _custom_config(tmp_path: Path) -> Path:
    text = CONFIG.read_text(encoding="utf-8")
    replacements = {
        "output_dir: runs/gemma4-multimodal": f"output_dir: {tmp_path.as_posix()}/runs",
        "smoke_steps: 20": "smoke_steps: 3",
        "simulation_seed: 20260710": "simulation_seed: 4242",
        "train_examples: 120": "train_examples: 7",
        "eval_examples: 12": "eval_examples: 3",
    }
    for source, replacement in replacements.items():
        text = text.replace(source, replacement)
    path = tmp_path / "training.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_simulation_uses_configurable_counts_and_shared_generators(tmp_path: Path) -> None:
    config = load_training_config(CONFIG)

    report = build_shakedown_datasets(
        output_dir=tmp_path / "datasets",
        repo_root=ROOT,
        seed=99,
        train_examples=5,
        eval_examples=2,
        mixture_weights=config.shakedown.mixture_weights,
    )

    assert report["train_summary"]["row_count"] == 5
    assert report["eval_summary"]["row_count"] == 2
    assert report["source_generators"] == ["shelfwise_worldgen", "shelfwise_synthdata"]
    first_row = json.loads(Path(report["train_path"]).read_text(encoding="utf-8").splitlines()[0])
    metadata = first_row["evidence"][-1]["metadata"]["world_event"]
    assert metadata["canonical_world_event"]["source"]
    assert metadata["synthetic_golden_scenario"]["invariants"]


def test_shakedown_passes_config_values_to_dataset_and_training(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _custom_config(tmp_path)
    observed: dict[str, Any] = {"training": []}

    def fake_datasets(**kwargs: Any) -> dict[str, Any]:
        observed["dataset"] = kwargs
        return {
            "train_path": str(tmp_path / "train.jsonl"),
            "eval_path": str(tmp_path / "eval.jsonl"),
            "mixture_weights": kwargs["mixture_weights"],
            "train_summary": {"row_count": kwargs["train_examples"]},
        }

    def fake_training(
        _config_path: str | Path,
        *,
        run_name: str,
        max_steps: int | None = None,
        train_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> Path:
        observed["training"].append(
            {"run_name": run_name, "max_steps": max_steps, "train_path": train_path}
        )
        run_dir = Path(output_dir or tmp_path) / run_name
        (run_dir / "final_adapter").mkdir(parents=True)
        return run_dir

    def fake_evaluation(
        _config_path: str | Path,
        **kwargs: Any,
    ) -> Path:
        run_dir = Path(kwargs["output_dir"]) / "eval"
        run_dir.mkdir(parents=True)
        (run_dir / "eval_summary.json").write_text(
            json.dumps({"gate": {"passed": True}}),
            encoding="utf-8",
        )
        return run_dir

    monkeypatch.setattr(
        "shelfwise.training.shakedown.build_shakedown_datasets", fake_datasets
    )
    monkeypatch.setattr("shelfwise.training.shakedown.run_training", fake_training)
    monkeypatch.setattr("shelfwise.training.shakedown.run_evaluation", fake_evaluation)

    run_shakedown(
        config_path,
        run_name="config-wiring",
        skip_preflight=True,
        skip_serving_check=True,
    )

    assert observed["dataset"]["seed"] == 4242
    assert observed["dataset"]["train_examples"] == 7
    assert observed["dataset"]["eval_examples"] == 3
    assert observed["training"][0]["max_steps"] == 3
    assert observed["training"][1]["max_steps"] is None
