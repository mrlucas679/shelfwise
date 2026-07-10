from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime

from .models import (
    BenchmarkConfig,
    EvidenceScope,
    RequestMetric,
    SkippedStrategy,
    StrategyComparison,
    StrategyKind,
    TelemetrySample,
    TelemetryStatus,
    WindowResult,
    WorkflowResult,
    WorkflowSpec,
    WorkloadStage,
)


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Return a linearly interpolated percentile for finite values."""

    ordered = sorted(float(value) for value in values if math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_workflow(
    *,
    run_id: str,
    strategy: str,
    strategy_kind: str,
    stage: WorkloadStage,
    repeat: int,
    workflow_id: str,
    workflow: WorkflowSpec,
    requests: list[RequestMetric],
    completion_ms: float,
    telemetry: list[TelemetrySample],
    scope: EvidenceScope,
) -> WorkflowResult:
    """Aggregate request and shared-window telemetry for one workflow."""

    model_requests = [item for item in requests if item.model_call]
    latencies = _values(model_requests, "latency_ms")
    queue_wait = _complete_sum(model_requests, "queue_time_ms")
    quality, quality_status = _quality(workflow, requests)
    resources = _resource_summary(telemetry, scope)
    expected_agents = {agent.name for agent in workflow.agents}
    completed_agents = {item.agent for item in requests if item.success}
    completed = expected_agents == completed_agents and not any(not item.success for item in requests)
    model_calls = len(model_requests)
    return WorkflowResult(
        run_id=run_id,
        strategy=strategy,
        strategy_kind=strategy_kind,
        stage=stage.name,
        repeat=repeat,
        workflow_id=workflow_id,
        agents=tuple(agent.name for agent in workflow.agents),
        agent_order=tuple(agent.name for agent in sorted(workflow.agents, key=lambda item: item.order)),
        parallelism=parallelism_layout(workflow, stage.synchronize_all_agents),
        synchronized_all_agents=stage.synchronize_all_agents,
        concurrency=stage.workflow_concurrency,
        max_parallel_model_calls=max_parallelism(workflow, stage.synchronize_all_agents),
        model_calls=model_calls,
        input_tokens=_complete_sum(model_requests, "prompt_tokens"),
        output_tokens=_complete_sum(model_requests, "completion_tokens"),
        total_tokens=_complete_sum(model_requests, "total_tokens"),
        avg_latency_ms=_mean(latencies),
        peak_latency_ms=max(latencies, default=None),
        p50_latency_ms=percentile(latencies, 0.50),
        p95_latency_ms=percentile(latencies, 0.95),
        p99_latency_ms=percentile(latencies, 0.99),
        rps=None if model_calls == 0 else model_calls / max(completion_ms / 1000, 0.000001),
        gpu_util_avg_pct=resources["gpu_avg"],
        gpu_util_peak_pct=resources["gpu_peak"],
        vram_avg_mb=resources["vram_avg"],
        vram_peak_mb=resources["vram_peak"],
        cpu_avg_pct=resources["cpu_avg"],
        cpu_peak_pct=resources["cpu_peak"],
        ram_avg_mb=resources["ram_avg"],
        ram_peak_mb=resources["ram_peak"],
        queue_length_avg=resources["queue_avg"],
        queue_length_peak=resources["queue_peak"],
        idle_time_ms=resources["idle_ms"],
        inference_wait_ms=queue_wait,
        workflow_completion_ms=completion_ms,
        workflow_completed=completed,
        quality_score=quality,
        gpu_telemetry_status=resources["gpu_status"],
        host_telemetry_status=resources["host_status"],
        queue_telemetry_status=resources["queue_status"],
        inference_wait_status=_request_metric_status(model_requests, "queue_time_ms"),
        quality_status=quality_status,
        telemetry_scope=scope.value,
        error_count=sum(not item.success for item in requests),
    )


def build_strategy_comparisons(
    config: BenchmarkConfig,
    workflows: list[WorkflowResult],
    requests: list[RequestMetric],
    windows: list[WindowResult],
    telemetry: list[TelemetrySample],
    skipped: list[SkippedStrategy],
    scope: EvidenceScope,
) -> list[StrategyComparison]:
    """Build per-stage strategy tradeoffs and shared-baseline deltas."""

    comparisons: list[StrategyComparison] = []
    skipped_by_name = {item.strategy: item.reason for item in skipped}
    for strategy in config.strategies:
        for stage in config.settings.stages():
            selected_windows = _select(windows, strategy.name, stage.name)
            if not selected_windows:
                comparisons.append(
                    _empty_comparison(strategy.name, strategy.kind.value, stage, skipped_by_name)
                )
                continue
            selected_workflows = _select(workflows, strategy.name, stage.name)
            selected_requests = _select(requests, strategy.name, stage.name)
            selected_telemetry = _select(telemetry, strategy.name, stage.name)
            comparisons.append(
                _comparison_from_measurements(
                    strategy.name,
                    strategy.kind.value,
                    stage,
                    selected_workflows,
                    selected_requests,
                    selected_windows,
                    selected_telemetry,
                    scope,
                )
            )
    _attach_shared_deltas(comparisons, scope)
    return comparisons


def parallelism_layout(workflow: WorkflowSpec, synchronized: bool) -> str:
    """Return a compact, stable description of agent execution groups."""

    if synchronized:
        return "synchronized_all:" + ",".join(agent.name for agent in workflow.agents)
    groups: dict[int, list[str]] = defaultdict(list)
    for agent in workflow.agents:
        groups[agent.order].append(agent.name)
    return ";".join(f"order_{order}:{','.join(groups[order])}" for order in sorted(groups))


def max_parallelism(workflow: WorkflowSpec, synchronized: bool) -> int:
    """Return the maximum model calls one workflow can issue together."""

    if synchronized:
        return len(workflow.agents)
    counts = Counter(agent.order for agent in workflow.agents)
    return max(counts.values(), default=0)


def _comparison_from_measurements(
    strategy: str,
    strategy_kind: str,
    stage: WorkloadStage,
    workflows: list[WorkflowResult],
    requests: list[RequestMetric],
    windows: list[WindowResult],
    telemetry: list[TelemetrySample],
    scope: EvidenceScope,
) -> StrategyComparison:
    """Aggregate measured rows into one strategy-stage comparison."""

    model_requests = [item for item in requests if item.model_call]
    latencies = _values(model_requests, "latency_ms")
    elapsed_seconds = sum(item.elapsed_ms for item in windows) / 1000
    started = sum(item.workflows_started for item in windows)
    completed = sum(item.workflows_completed for item in windows)
    resources = _resource_summary(telemetry, scope)
    quality_values = _values(model_requests, "quality_score")
    has_failures = any(not item.success for item in requests)
    status = "control_plane_only" if not model_requests else "partial" if has_failures else "measured"
    return StrategyComparison(
        strategy=strategy,
        strategy_kind=strategy_kind,
        stage=stage.name,
        concurrency=stage.workflow_concurrency,
        measurement_status=status,
        workflows_started=started,
        workflows_completed=completed,
        completion_rate=None if started == 0 else completed / started,
        model_calls=len(model_requests),
        input_tokens=_complete_sum(model_requests, "prompt_tokens"),
        output_tokens=_complete_sum(model_requests, "completion_tokens"),
        total_tokens=_complete_sum(model_requests, "total_tokens"),
        avg_latency_ms=_mean(latencies),
        peak_latency_ms=max(latencies, default=None),
        p50_latency_ms=percentile(latencies, 0.50),
        p95_latency_ms=percentile(latencies, 0.95),
        p99_latency_ms=percentile(latencies, 0.99),
        request_rps=None if not model_requests else len(model_requests) / max(elapsed_seconds, 0.000001),
        workflow_rps=completed / max(elapsed_seconds, 0.000001),
        quality_score=_mean(quality_values),
        gpu_util_avg_pct=resources["gpu_avg"],
        gpu_util_peak_pct=resources["gpu_peak"],
        vram_peak_mb=resources["vram_peak"],
        cpu_avg_pct=resources["cpu_avg"],
        ram_peak_mb=resources["ram_peak"],
        queue_length_avg=resources["queue_avg"],
        queue_length_peak=resources["queue_peak"],
        idle_time_ms=resources["idle_ms"],
        inference_wait_ms=_complete_sum(model_requests, "queue_time_ms"),
        notes=_comparison_notes(resources, quality_values, scope),
    )


def _empty_comparison(
    strategy: str,
    strategy_kind: str,
    stage: WorkloadStage,
    skipped: dict[str, str],
) -> StrategyComparison:
    """Create an unavailable row for a strategy that failed preflight."""

    return StrategyComparison(
        strategy=strategy,
        strategy_kind=strategy_kind,
        stage=stage.name,
        concurrency=stage.workflow_concurrency,
        measurement_status="unavailable",
        workflows_started=0,
        workflows_completed=0,
        completion_rate=None,
        model_calls=0,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        avg_latency_ms=None,
        peak_latency_ms=None,
        p50_latency_ms=None,
        p95_latency_ms=None,
        p99_latency_ms=None,
        request_rps=None,
        workflow_rps=None,
        quality_score=None,
        gpu_util_avg_pct=None,
        gpu_util_peak_pct=None,
        vram_peak_mb=None,
        cpu_avg_pct=None,
        ram_peak_mb=None,
        queue_length_avg=None,
        queue_length_peak=None,
        idle_time_ms=None,
        inference_wait_ms=None,
        notes=skipped.get(strategy, "strategy was not measured"),
    )


def _attach_shared_deltas(
    comparisons: list[StrategyComparison],
    scope: EvidenceScope,
) -> None:
    """Attach metric deltas against shared strategy rows without ranking."""

    shared = {
        item.stage: item
        for item in comparisons
        if item.strategy_kind == StrategyKind.SHARED.value and item.measurement_status != "unavailable"
    }
    for item in comparisons:
        baseline = shared.get(item.stage)
        if baseline is None:
            continue
        item.quality_delta_vs_shared = _difference(item.quality_score, baseline.quality_score)
        item.p95_latency_ratio_vs_shared = _ratio(item.p95_latency_ms, baseline.p95_latency_ms)
        item.request_rps_ratio_vs_shared = _ratio(item.request_rps, baseline.request_rps)
        item.workflow_rps_ratio_vs_shared = _ratio(item.workflow_rps, baseline.workflow_rps)
        if scope is EvidenceScope.CLOUD_INFERENCE_HOST:
            item.gpu_util_ratio_vs_shared = _ratio(
                item.gpu_util_avg_pct,
                baseline.gpu_util_avg_pct,
            )
            item.vram_ratio_vs_shared = _ratio(item.vram_peak_mb, baseline.vram_peak_mb)


def _resource_summary(
    telemetry: list[TelemetrySample],
    scope: EvidenceScope,
) -> dict[str, float | str | None]:
    """Summarize resource samples and preserve source availability states."""

    gpu = _telemetry_values(telemetry, "amd_smi", "gpu_util_pct")
    vram = _telemetry_values(telemetry, "amd_smi", "vram_used_mb")
    cpu = _telemetry_values(telemetry, "host", "cpu_util_pct")
    ram = _telemetry_values(telemetry, "host", "ram_used_mb")
    queue = _telemetry_values(telemetry, "vllm_metrics", "queue_length")
    return {
        "gpu_avg": _mean(gpu),
        "gpu_peak": max(gpu, default=None),
        "vram_avg": _mean(vram),
        "vram_peak": max(vram, default=None),
        "cpu_avg": _mean(cpu),
        "cpu_peak": max(cpu, default=None),
        "ram_avg": _mean(ram),
        "ram_peak": max(ram, default=None),
        "queue_avg": _mean(queue),
        "queue_peak": max(queue, default=None),
        "idle_ms": _idle_time_ms(telemetry),
        "gpu_status": TelemetryStatus.MEASURED.value
        if gpu or vram
        else TelemetryStatus.UNAVAILABLE.value,
        "host_status": TelemetryStatus.CONTROL_PLANE_ONLY.value
        if scope is EvidenceScope.CONTROL_PLANE_ONLY and (cpu or ram)
        else TelemetryStatus.MEASURED.value
        if cpu or ram
        else TelemetryStatus.UNAVAILABLE.value,
        "queue_status": TelemetryStatus.MEASURED.value
        if queue
        else TelemetryStatus.UNAVAILABLE.value,
    }


def _idle_time_ms(telemetry: list[TelemetrySample]) -> float | None:
    """Integrate intervals where vLLM reports no running or waiting requests."""

    by_repeat: dict[int, dict[str, list[TelemetrySample]]] = defaultdict(lambda: defaultdict(list))
    for sample in telemetry:
        if sample.source == "vllm_metrics" and sample.available:
            by_repeat[sample.repeat][sample.timestamp].append(sample)
    if not by_repeat:
        return None
    total_ms = 0.0
    interval_count = 0
    for timestamps in by_repeat.values():
        ordered = sorted(timestamps)
        for current, following in zip(ordered, ordered[1:], strict=False):
            group = timestamps[current]
            if any(item.queue_length is None or item.running_requests is None for item in group):
                continue
            interval_count += 1
            if sum(item.queue_length or 0 for item in group) == 0 and sum(
                item.running_requests or 0 for item in group
            ) == 0:
                delta = datetime.fromisoformat(following) - datetime.fromisoformat(current)
                total_ms += delta.total_seconds() * 1000
    return total_ms if interval_count else None


def _quality(
    workflow: WorkflowSpec,
    requests: list[RequestMetric],
) -> tuple[float | None, str]:
    """Aggregate deterministic quality checks and label partial coverage."""

    expected_agents = {agent.name for agent in workflow.agents if agent.expected_terms}
    if not expected_agents:
        return None, TelemetryStatus.NOT_APPLICABLE.value
    values = [item.quality_score for item in requests if item.agent in expected_agents]
    measured = [value for value in values if value is not None]
    if not measured:
        return None, TelemetryStatus.UNAVAILABLE.value
    status = (
        TelemetryStatus.MEASURED.value
        if len(measured) == len(expected_agents)
        else TelemetryStatus.PARTIAL.value
    )
    return statistics.fmean(measured), status


def _comparison_notes(
    resources: dict[str, float | str | None],
    quality_values: list[float],
    scope: EvidenceScope,
) -> str:
    """Describe evidence gaps that materially affect tradeoff interpretation."""

    notes: list[str] = []
    if scope is EvidenceScope.CONTROL_PLANE_ONLY:
        notes.append("CPU/RAM are control-plane-only; GPU/VRAM are not local AMD evidence")
    if resources["queue_status"] == TelemetryStatus.UNAVAILABLE.value:
        notes.append("vLLM queue telemetry unavailable")
    if resources["gpu_status"] == TelemetryStatus.UNAVAILABLE.value:
        notes.append("AMD-SMI GPU telemetry unavailable")
    if not quality_values:
        notes.append("quality rubric unavailable")
    return "; ".join(notes)


def _request_metric_status(requests: list[RequestMetric], field: str) -> str:
    """Classify request metric coverage across model calls."""

    if not requests:
        return TelemetryStatus.NOT_APPLICABLE.value
    measured = sum(getattr(item, field) is not None for item in requests)
    if measured == len(requests):
        return TelemetryStatus.MEASURED.value
    if measured:
        return TelemetryStatus.PARTIAL.value
    return TelemetryStatus.UNAVAILABLE.value


def _telemetry_values(
    telemetry: Iterable[TelemetrySample],
    source: str,
    field: str,
) -> list[float]:
    """Return available numeric values for one telemetry source and field."""

    values = []
    for item in telemetry:
        value = getattr(item, field)
        if item.source == source and item.available and value is not None:
            values.append(float(value))
    return values


def _values(items: Iterable[object], field: str) -> list[float]:
    """Return finite numeric values for a dataclass field."""

    values = []
    for item in items:
        value = getattr(item, field)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    return values


def _complete_sum(items: Sequence[object], field: str) -> int | float | None:
    """Sum a metric only when every applicable row supplied it."""

    if not items:
        return None
    values = [getattr(item, field) for item in items]
    if any(value is None for value in values):
        return None
    return sum(values)


def _mean(values: Sequence[float]) -> float | None:
    """Return a mean for non-empty values."""

    return statistics.fmean(values) if values else None


def _ratio(value: float | None, baseline: float | None) -> float | None:
    """Return a ratio only when the baseline is positive."""

    if value is None or baseline is None or baseline <= 0:
        return None
    return value / baseline


def _difference(value: float | None, baseline: float | None) -> float | None:
    """Return a signed difference when both metrics are available."""

    if value is None or baseline is None:
        return None
    return value - baseline


def _select(items: Iterable[object], strategy: str, stage: str) -> list:
    """Select benchmark rows by strategy and stage labels."""

    return [item for item in items if item.strategy == strategy and item.stage == stage]
