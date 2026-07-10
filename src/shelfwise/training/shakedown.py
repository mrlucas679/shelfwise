from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_training_config
from .evaluate import run_evaluation
from .preflight import run_preflight
from .runtime import git_commit, timestamped_run_dir, write_json
from .serving_check import run_serving_check
from .simulation import build_shakedown_datasets
from .train import run_training


def run_shakedown(
    config_path: str | Path,
    *,
    run_name: str,
    dry_run: bool = False,
    skip_preflight: bool = False,
    skip_training: bool = False,
    skip_serving_check: bool = False,
    existing_adapter_path: str | Path | None = None,
) -> Path:
    """Run the gated full ShelfWise AI shakedown pipeline."""

    repo_root = Path.cwd()
    config = load_training_config(config_path)
    run_dir = timestamped_run_dir(
        repo_root / config.output_dir / "shakedown",
        run_name,
        timestamp=config.safety.timestamp_output_dir,
    )
    datasets_dir = run_dir / "datasets"
    report: dict[str, Any] = {
        "run_name": run_name,
        "git_commit": git_commit(repo_root),
        "model_profile": config.profile_name,
        "model_name": config.model_name_or_path,
        "model_revision": config.model_revision,
        "processor_class": "Gemma4UnifiedProcessor",
        "lora_target_modules": list(config.lora.target_modules),
        "max_seq_length": config.max_seq_length,
        "runtime_boundary": {
            "training_target": config.runtime.training_target,
            "serving_target": config.runtime.serving_target,
        },
        "shakedown_settings": {
            "smoke_steps": config.shakedown.smoke_steps,
            "simulation_seed": config.shakedown.simulation_seed,
            "train_examples": config.shakedown.train_examples,
            "eval_examples": config.shakedown.eval_examples,
            "mixture_weights": config.shakedown.mixture_weights,
        },
        "stages": [],
        "known_limitations": [
            "Audio is transcript-based unless the processor receives raw audio tensors.",
            "Video is frame-sampled unless the processor receives raw video tensors.",
        ],
    }
    dataset_report = build_shakedown_datasets(
        output_dir=datasets_dir,
        repo_root=repo_root,
        seed=config.shakedown.simulation_seed,
        train_examples=config.shakedown.train_examples,
        eval_examples=config.shakedown.eval_examples,
        mixture_weights=config.shakedown.mixture_weights,
    )
    report["dataset"] = dataset_report
    report["stages"].append("simulation_dataset_generation")

    train_path = Path(dataset_report["train_path"])
    eval_path = Path(dataset_report["eval_path"])
    if dry_run:
        eval_dir = run_evaluation(
            config_path,
            dry_run=True,
            eval_path=eval_path,
            output_dir=run_dir / "evaluation",
        )
        report["evaluation_dir"] = str(eval_dir)
        report["evaluation_summary"] = _load_eval_summary(eval_dir)
        report["training_evaluation_ready"] = False
        report["deployment_ready"] = False
        report["stages"].append("dry_run_eval")
        _write_final_report(run_dir, report)
        return run_dir

    if not skip_preflight:
        run_preflight(config_path, train_path=train_path)
        report["stages"].append("preflight")

    smoke_dir: Path | None = None
    full_dir: Path | None = None
    if not skip_training:
        smoke_dir = run_training(
            config_path,
            run_name="smoke",
            max_steps=config.shakedown.smoke_steps,
            train_path=train_path,
            output_dir=run_dir / "training",
        )
        report["smoke_training_dir"] = str(smoke_dir)
        report["stages"].append("smoke_train")

        full_dir = run_training(
            config_path,
            run_name="full",
            train_path=train_path,
            output_dir=run_dir / "training",
        )
        report["full_training_dir"] = str(full_dir)
        report["final_adapter_path"] = str(full_dir / "final_adapter")
        report["stages"].append("full_train")

    selected_adapter = (
        (full_dir or smoke_dir) / "final_adapter"
        if (full_dir or smoke_dir)
        else Path(existing_adapter_path) if existing_adapter_path else None
    )
    eval_dir = run_evaluation(
        config_path,
        dry_run=selected_adapter is None,
        eval_path=eval_path,
        output_dir=run_dir / "evaluation",
        adapter_path=selected_adapter,
    )
    report["evaluation_dir"] = str(eval_dir)
    evaluation_summary = _load_eval_summary(eval_dir)
    report["evaluation_summary"] = evaluation_summary
    report["training_evaluation_ready"] = evaluation_summary["gate"]["passed"]
    report["stages"].append(
        "generated_evaluation" if selected_adapter is not None else "fixture_only_evaluation"
    )

    if selected_adapter is not None and not skip_serving_check:
        serving = run_serving_check(
            config_path,
            adapter_path=selected_adapter,
            mode=config.serving.gate_mode,
        )
        report["serving_check"] = serving
        report["deployment_ready"] = serving["gate"]["deployment_ready"]
        report["stages"].append(f"serving_check_{config.serving.gate_mode}")
    else:
        report["deployment_ready"] = False

    _write_final_report(run_dir, report)
    return run_dir


def _write_final_report(run_dir: Path, report: dict[str, Any]) -> None:
    write_json(run_dir / "shakedown_report.json", report)
    lines = [
        "# ShelfWise Gemma 4 Multimodal Shakedown",
        "",
        f"- Run name: `{report['run_name']}`",
        f"- Git commit: `{report.get('git_commit')}`",
        f"- Model: `{report['model_name']}`",
        f"- Model profile: `{report['model_profile']}`",
        f"- Model revision: `{report['model_revision']}`",
        f"- Processor: `{report['processor_class']}`",
        f"- Max sequence length: `{report['max_seq_length']}`",
        f"- Stages completed: `{', '.join(report['stages'])}`",
        "",
        "## Dataset",
        "",
        f"- Train path: `{report['dataset']['train_path']}`",
        f"- Eval path: `{report['dataset']['eval_path']}`",
        f"- Mixture: `{json.dumps(report['dataset']['mixture_weights'], sort_keys=True)}`",
        f"- Train summary: `{json.dumps(report['dataset']['train_summary'], sort_keys=True)}`",
        "",
        "## Outputs",
        "",
        f"- Smoke training dir: `{report.get('smoke_training_dir', 'not run')}`",
        f"- Full training dir: `{report.get('full_training_dir', 'not run')}`",
        f"- Final adapter path: `{report.get('final_adapter_path', 'not produced')}`",
        f"- Evaluation dir: `{report.get('evaluation_dir', 'not run')}`",
        f"- Generated evaluation ready: `{report.get('training_evaluation_ready', False)}`",
        f"- MI300X deployment ready: `{report.get('deployment_ready', False)}`",
        "",
        "## Known Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["known_limitations"])
    lines.extend(
        [
            "",
            "## Recommended Next Improvements",
            "",
            "- Replace transcript/frame fallbacks with raw processor tensors once verified.",
            "- Deploy the compatible adapter to the MI300X endpoint and run generated_inference.",
            "- Pin a model revision commit instead of `main` before a release candidate run.",
        ]
    )
    (run_dir / "shakedown_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"shakedown report: {run_dir / 'shakedown_report.md'}")


def _load_eval_summary(eval_dir: Path) -> dict[str, Any]:
    return json.loads((eval_dir / "eval_summary.json").read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Full ShelfWise Gemma 4 multimodal shakedown")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run_name", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-training", action="store_true")
    parser.add_argument("--skip-serving-check", action="store_true")
    parser.add_argument("--adapter-path")
    args = parser.parse_args()
    run_shakedown(
        args.config,
        run_name=args.run_name,
        dry_run=args.dry_run,
        skip_preflight=args.skip_preflight,
        skip_training=args.skip_training,
        skip_serving_check=args.skip_serving_check,
        existing_adapter_path=args.adapter_path,
    )


if __name__ == "__main__":
    main()
