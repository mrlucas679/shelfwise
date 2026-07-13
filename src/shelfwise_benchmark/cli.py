from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from shelfwise_runtime import durable_dir

from .config import load_benchmark_config
from .models import BenchmarkConfig, EvidenceScope, RunSettings
from .reporting import write_benchmark_outputs
from .runner import BenchmarkRunner
from .telemetry import AmdSmiSampler

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "reports" / "templates" / "inference_architecture_benchmark.json"
DEFAULT_REPORT_TEMPLATE = (
    REPO_ROOT / "reports" / "templates" / "inference_architecture_report.md.tmpl"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the cloud inference architecture benchmark CLI."""

    parser = argparse.ArgumentParser(
        description=(
            "Benchmark shared, replicated, per-agent, and hybrid vLLM architectures. "
            "The command never starts the ShelfWise app or a local model server."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--execution-scope",
        choices=[item.value for item in EvidenceScope],
        default=EvidenceScope.CONTROL_PLANE_ONLY.value,
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Exercise routing and reports without model calls or inference claims.",
    )
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument("--peak", type=int)
    parser.add_argument("--synchronized-workflows", type=int)
    parser.add_argument("--warmup-seconds", type=float)
    parser.add_argument("--steady-seconds", type=float)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--telemetry-interval-seconds", type=float)
    parser.add_argument("--max-workflows-per-window", type=int)
    parser.add_argument("--amd-smi-binary", default="amd-smi")
    parser.add_argument("--validate-config", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate configuration, run the benchmark, and write artifacts."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_benchmark_config(args.config)
        config = _apply_overrides(config, args)
        if args.validate_config:
            print(_validation_summary(config))
            return 0
        scope = EvidenceScope(args.execution_scope)
        output_dir = args.output_dir or _default_output_dir()
        runner = BenchmarkRunner(
            config,
            scope=scope,
            plan_only=args.plan_only,
            amd_smi_sampler=AmdSmiSampler(args.amd_smi_binary),
        )
        result = asyncio.run(runner.run())
        artifacts = write_benchmark_outputs(
            result,
            output_dir,
            template_path=DEFAULT_REPORT_TEMPLATE,
        )
    except (OSError, ValueError) as exc:
        print(f"Benchmark configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Benchmark failed safely: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(artifacts["report_markdown"].resolve())
    return 2 if result.skipped_strategies and not result.windows else 0


def _apply_overrides(config: BenchmarkConfig, args: argparse.Namespace) -> BenchmarkConfig:
    """Apply explicit CLI workload and strategy filters to loaded config."""

    settings = config.settings
    values = {
        "peak_concurrency": args.peak,
        "synchronized_workflows": args.synchronized_workflows,
        "warmup_seconds": args.warmup_seconds,
        "steady_seconds": args.steady_seconds,
        "repeats": args.repeats,
        "telemetry_interval_seconds": args.telemetry_interval_seconds,
        "max_workflows_per_window": args.max_workflows_per_window,
    }
    overrides = {key: value for key, value in values.items() if value is not None}
    settings = replace(settings, **overrides)
    _validate_settings(settings)
    strategies = config.strategies
    if args.strategy:
        selected = set(args.strategy)
        strategies = tuple(item for item in strategies if item.name in selected)
        missing = selected.difference(item.name for item in strategies)
        if missing:
            raise ValueError(f"Unknown strategies: {', '.join(sorted(missing))}")
    return replace(config, strategies=strategies, settings=settings)


def _validate_settings(settings: RunSettings) -> None:
    """Validate CLI workload overrides before any network requests."""

    if settings.peak_concurrency <= 0 or settings.synchronized_workflows <= 0:
        raise ValueError("peak and synchronized workflow concurrency must be positive")
    if settings.warmup_seconds < 0 or settings.steady_seconds <= 0:
        raise ValueError("warmup must be non-negative and steady duration positive")
    if settings.repeats <= 0 or settings.telemetry_interval_seconds <= 0:
        raise ValueError("repeats and telemetry interval must be positive")
    if settings.max_workflows_per_window is not None and settings.max_workflows_per_window <= 0:
        raise ValueError("max workflows per window must be positive")


def _validation_summary(config: BenchmarkConfig) -> str:
    """Return a secret-free one-line configuration summary."""

    kinds = ", ".join(strategy.kind.value for strategy in config.strategies)
    return (
        f"valid workflow={config.workflow.name} agents={len(config.workflow.agents)} "
        f"strategies={len(config.strategies)} kinds=[{kinds}]"
    )


def _default_output_dir() -> Path:
    """Return a timestamped report directory without overwriting prior runs."""

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return durable_dir("BENCHMARK_OUTPUT_DIR", "reports") / f"inference_architecture_eval_{run_id}"


if __name__ == "__main__":
    raise SystemExit(main())
