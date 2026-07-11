from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from pathlib import Path
from string import Template
from typing import Any

from .models import (
    BenchmarkResult,
    EvidenceScope,
    RequestMetric,
    StrategyComparison,
    TelemetrySample,
    WindowResult,
    WorkflowResult,
)

_STALE_ROW_MARKERS = ("ollama", "direct_local_model", "local_model_probe")
_DEFAULT_TEMPLATE = """# Cloud Inference Architecture Benchmark

> Run `$run_id` | `$created_at` | Evidence scope: `$evidence_scope`

$evidence_notice

## Workload

$workload_table

Warmup: $warmup_seconds seconds. Steady window: $steady_seconds seconds. Repeats: $repeats.

## Measured Strategies

$strategy_table

## Tradeoffs Relative To Shared

No winner is selected. These deltas expose quality, latency, throughput, and resource tradeoffs.

$tradeoff_table

## Telemetry Availability

$telemetry_table

## Evidence Warnings

$warnings

## Output Files

$output_files
"""


def write_benchmark_outputs(
    result: BenchmarkResult,
    output_dir: Path,
    *,
    template_path: Path | None = None,
) -> dict[str, Path]:
    """Write CSV, JSON, Markdown, and graph-ready benchmark artifacts.

    Args:
        result: Completed benchmark evidence and comparisons.
        output_dir: Destination directory for generated artifacts.
        template_path: Optional Markdown `string.Template` source.

    Returns:
        A mapping from artifact purpose to its written path.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_local_rows(result)
    artifacts = {
        "requests_csv": output_dir / "request_metrics.csv",
        "workflows_csv": output_dir / "workflow_results.csv",
        "windows_csv": output_dir / "window_results.csv",
        "telemetry_csv": output_dir / "telemetry_samples.csv",
        "comparisons_csv": output_dir / "strategy_comparison.csv",
        "benchmark_json": output_dir / "benchmark.json",
        "graph_series_json": output_dir / "graph_series.json",
        "graph_series_csv": output_dir / "graph_series.csv",
        "report_markdown": output_dir / "benchmark.md",
    }
    _write_dataclass_csv(artifacts["requests_csv"], result.requests, RequestMetric)
    _write_dataclass_csv(artifacts["workflows_csv"], result.workflows, WorkflowResult)
    _write_dataclass_csv(artifacts["windows_csv"], result.windows, WindowResult)
    _write_dataclass_csv(artifacts["telemetry_csv"], result.telemetry, TelemetrySample)
    _write_dataclass_csv(
        artifacts["comparisons_csv"],
        result.comparisons,
        StrategyComparison,
    )
    graph_rows = graph_ready_series(result)
    _write_dict_csv(artifacts["graph_series_csv"], graph_rows)
    artifacts["graph_series_json"].write_text(
        json.dumps({"series": graph_rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifacts["benchmark_json"].write_text(
        json.dumps(asdict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    template = _load_template(template_path)
    report = template.safe_substitute(_report_context(result, artifacts))
    artifacts["report_markdown"].write_text(report.rstrip() + "\n", encoding="utf-8")
    return artifacts


def graph_ready_series(result: BenchmarkResult) -> list[dict[str, Any]]:
    """Return long-form strategy-stage metric rows suitable for plotting."""

    metrics = (
        ("completion_rate", "ratio"),
        ("quality_score", "ratio"),
        ("avg_latency_ms", "ms"),
        ("p50_latency_ms", "ms"),
        ("p95_latency_ms", "ms"),
        ("p99_latency_ms", "ms"),
        ("request_rps", "requests/second"),
        ("workflow_rps", "workflows/second"),
        ("gpu_util_avg_pct", "percent"),
        ("gpu_util_peak_pct", "percent"),
        ("vram_peak_mb", "MB"),
        ("cpu_avg_pct", "percent"),
        ("ram_peak_mb", "MB"),
        ("queue_length_avg", "requests"),
        ("queue_length_peak", "requests"),
        ("idle_time_ms", "ms"),
        ("inference_wait_ms", "ms"),
    )
    rows: list[dict[str, Any]] = []
    for comparison in result.comparisons:
        for metric, unit in metrics:
            value = getattr(comparison, metric)
            rows.append(
                {
                    "run_id": result.run_id,
                    "evidence_scope": result.evidence_scope,
                    "strategy": comparison.strategy,
                    "strategy_kind": comparison.strategy_kind,
                    "stage": comparison.stage,
                    "concurrency": comparison.concurrency,
                    "metric": metric,
                    "unit": unit,
                    "value": value,
                    "available": value is not None,
                    "measurement_status": comparison.measurement_status,
                }
            )
    return rows


def _remove_stale_local_rows(result: BenchmarkResult) -> None:
    """Remove historical local-model data rows from every generated artifact."""

    collections = (
        "requests",
        "workflows",
        "windows",
        "telemetry",
        "comparisons",
        "skipped_strategies",
    )
    removed = 0
    for name in collections:
        rows = getattr(result, name)
        retained = [row for row in rows if not _is_stale_row(row)]
        removed += len(rows) - len(retained)
        setattr(result, name, retained)
    result.excluded_stale_rows += removed


def _is_stale_row(row: object) -> bool:
    """Detect only explicit historical local-provider identifiers in data rows."""

    identifiers = []
    for field_name in ("strategy", "endpoint", "model", "provider", "target"):
        value = getattr(row, field_name, "")
        if isinstance(value, str):
            identifiers.append(value.casefold())
    combined = " ".join(identifiers)
    return any(marker in combined for marker in _STALE_ROW_MARKERS)


def _write_dataclass_csv(path: Path, rows: list, row_type: type) -> None:
    """Write dataclass rows with stable headers and flattened tuple values."""

    names = [item.name for item in fields(row_type)]
    dictionaries = [_flatten_row(asdict(row)) for row in rows]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(dictionaries)


def _write_dict_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write homogeneous dictionaries as a UTF-8 CSV artifact."""

    fieldnames = list(rows[0]) if rows else ["run_id", "metric", "value", "available"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten sequence values for readable CSV cells."""

    return {
        key: " > ".join(str(item) for item in value) if isinstance(value, tuple | list) else value
        for key, value in row.items()
    }


def _load_template(path: Path | None) -> Template:
    """Load a report template or use the dependency-free built-in form."""

    if path is None or not path.exists():
        return Template(_DEFAULT_TEMPLATE)
    return Template(path.read_text(encoding="utf-8"))


def _report_context(
    result: BenchmarkResult,
    artifacts: dict[str, Path],
) -> dict[str, str | int | float]:
    """Build Markdown template values from benchmark evidence."""

    settings = result.settings
    return {
        "run_id": result.run_id,
        "created_at": result.created_at,
        "evidence_scope": result.evidence_scope,
        "evidence_notice": _evidence_notice(result.evidence_scope),
        "workload_table": _workload_table(settings.get("stages", [])),
        "warmup_seconds": settings.get("warmup_seconds", "unavailable"),
        "steady_seconds": settings.get("steady_seconds", "unavailable"),
        "repeats": settings.get("repeats", "unavailable"),
        "strategy_table": _strategy_table(result.comparisons),
        "tradeoff_table": _tradeoff_table(result.comparisons),
        "telemetry_table": _telemetry_table(result.telemetry),
        "warnings": _warning_list(result.warnings),
        "output_files": "\n".join(f"- `{path.name}`" for path in artifacts.values()),
    }


def _evidence_notice(scope: str) -> str:
    """Return the non-negotiable hardware evidence boundary."""

    if scope == EvidenceScope.CONTROL_PLANE_ONLY.value:
        return (
            "**Control-plane-only run.** Request timing and remote vLLM `/metrics` may be "
            "measured, but local CPU/RAM are labelled control-plane telemetry. Local GPU and "
            "VRAM are disabled and are not AMD cloud evidence."
        )
    return (
        "**Cloud inference host run.** AMD-SMI and host resource values are usable only for the "
        "declared host and this run window. Unavailable fields remain blank."
    )


def _workload_table(stages: list[dict[str, Any]]) -> str:
    """Render workload stages including synchronized worst-case behavior."""

    lines = ["| Stage | Workflow concurrency | All agents synchronized |", "|---|---:|:---:|"]
    for stage in stages:
        lines.append(
            f"| {stage['name']} | {stage['workflow_concurrency']} | "
            f"{'yes' if stage['synchronize_all_agents'] else 'no'} |"
        )
    return "\n".join(lines)


def _strategy_table(comparisons: list[StrategyComparison]) -> str:
    """Render primary quality, latency, throughput, and resource metrics."""

    lines = [
        "| Strategy | Kind | Stage | C | Status | Complete | Quality | P95 ms | Req/s | "
        "Workflow/s | GPU avg % | VRAM peak MB | Queue avg |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in comparisons:
        lines.append(
            f"| {item.strategy} | {item.strategy_kind} | {item.stage} | {item.concurrency} | "
            f"{item.measurement_status} | {_format(item.completion_rate)} | "
            f"{_format(item.quality_score)} | {_format(item.p95_latency_ms)} | "
            f"{_format(item.request_rps)} | {_format(item.workflow_rps)} | "
            f"{_format(item.gpu_util_avg_pct)} | {_format(item.vram_peak_mb)} | "
            f"{_format(item.queue_length_avg)} |"
        )
    return "\n".join(lines)


def _tradeoff_table(comparisons: list[StrategyComparison]) -> str:
    """Render neutral deltas against each stage's shared baseline."""

    lines = [
        "| Strategy | Stage | Quality delta | P95 ratio | Req/s ratio | Workflow/s ratio | "
        "GPU ratio | VRAM ratio | Notes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in comparisons:
        lines.append(
            f"| {item.strategy} | {item.stage} | {_format(item.quality_delta_vs_shared)} | "
            f"{_format(item.p95_latency_ratio_vs_shared)} | "
            f"{_format(item.request_rps_ratio_vs_shared)} | "
            f"{_format(item.workflow_rps_ratio_vs_shared)} | "
            f"{_format(item.gpu_util_ratio_vs_shared)} | "
            f"{_format(item.vram_ratio_vs_shared)} | {item.notes or 'none'} |"
        )
    return "\n".join(lines)


def _telemetry_table(samples: list[TelemetrySample]) -> str:
    """Render source-level availability and safe reason codes."""

    by_source: dict[str, list[TelemetrySample]] = {}
    for sample in samples:
        by_source.setdefault(sample.source, []).append(sample)
    lines = ["| Source | Scope | Available samples | Unavailable reasons |", "|---|---|---:|---|"]
    for source in ("vllm_metrics", "amd_smi", "host"):
        values = by_source.get(source, [])
        available = sum(item.available for item in values)
        scopes = ", ".join(sorted({item.scope for item in values})) or "unavailable"
        reasons = ", ".join(sorted({item.reason for item in values if item.reason})) or "none"
        lines.append(f"| {source} | {scopes} | {available} | {reasons} |")
    return "\n".join(lines)


def _warning_list(warnings: list[str]) -> str:
    """Render warnings as a Markdown list without inventing reassurance."""

    return "\n".join(f"- {warning}" for warning in warnings) if warnings else "- None recorded."


def _format(value: float | None) -> str:
    """Format measured values while keeping unavailable values explicit."""

    return "unavailable" if value is None else f"{value:.3f}"
