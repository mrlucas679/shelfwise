from __future__ import annotations

import asyncio
from collections import defaultdict

from shelfwise_benchmark.adapters import VllmMetricsSnapshot
from shelfwise_benchmark.models import (
    AgentSpec,
    BenchmarkConfig,
    EndpointSpec,
    EvidenceScope,
    RequestOutcome,
    RunSettings,
    StrategyKind,
    StrategySpec,
    WorkflowSpec,
)
from shelfwise_benchmark.runner import BenchmarkRunner
from shelfwise_benchmark.telemetry import HostMetric


class FakeInferenceAdapter:
    """Return deterministic provider metrics while tracking active requests."""

    def __init__(self) -> None:
        self.active: defaultdict[str, int] = defaultdict(int)
        self.maximum: defaultdict[str, int] = defaultdict(int)
        self.closed = False

    async def complete(self, endpoint, agent, request_id) -> RequestOutcome:
        stage = "synchronized" if "synchronized_all_agents" in request_id else "ordered"
        self.active[stage] += 1
        self.maximum[stage] = max(self.maximum[stage], self.active[stage])
        await asyncio.sleep(0.002)
        self.active[stage] -= 1
        return RequestOutcome(
            success=True,
            model_call=True,
            status_code=200,
            latency_ms=12.0,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            queue_time_ms=2.0,
            inference_time_ms=9.0,
            time_to_first_token_ms=4.0,
            generation_time_ms=5.0,
            mean_inter_token_latency_ms=1.0,
            tokens_per_second=50.0,
            quality_score=1.0,
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeMetricsClient:
    """Return queue metrics without requiring a live vLLM endpoint."""

    async def sample(self, endpoint) -> VllmMetricsSnapshot:
        return VllmMetricsSnapshot(
            endpoint=endpoint.name,
            available=True,
            queue_length=0.0,
            running_requests=0.0,
            queue_time_sum_seconds=1.0,
            queue_time_count=1.0,
            inference_time_sum_seconds=2.0,
            inference_time_count=1.0,
        )

    async def aclose(self) -> None:
        return None


class FakeHostSampler:
    """Return deterministic control-plane CPU and RAM metrics."""

    async def sample(self) -> HostMetric:
        return HostMetric(True, cpu_util_pct=25.0, ram_used_mb=1024.0)


def _config() -> BenchmarkConfig:
    agents = (
        AgentSpec("routine_agent", 1, "first", "routine", "routine", expected_terms=("ok",)),
        AgentSpec("strong_agent", 2, "second", "strong", "strong", expected_terms=("ok",)),
    )
    endpoints = {
        "a": EndpointSpec("a", "https://a.example/v1", "model-a"),
        "b": EndpointSpec("b", "https://b.example/v1", "model-b"),
    }
    strategies = (
        StrategySpec("shared", StrategyKind.SHARED, {"default": ("a",)}),
        StrategySpec("replicated", StrategyKind.REPLICATED, {"default": ("a", "b")}),
        StrategySpec(
            "per_agent",
            StrategyKind.PER_AGENT,
            {"routine_agent": ("a",), "strong_agent": ("b",)},
        ),
        StrategySpec(
            "hybrid",
            StrategyKind.HYBRID,
            {"routine": ("a",), "strong": ("b",)},
        ),
    )
    settings = RunSettings(
        peak_concurrency=2,
        synchronized_workflows=1,
        warmup_seconds=0,
        steady_seconds=0.005,
        repeats=1,
        telemetry_interval_seconds=0.1,
        max_workflows_per_window=1,
    )
    return BenchmarkConfig(WorkflowSpec("workflow", agents), endpoints, strategies, settings)


def test_runner_records_required_metrics_and_synchronized_worst_case() -> None:
    adapter = FakeInferenceAdapter()
    runner = BenchmarkRunner(
        _config(),
        scope=EvidenceScope.CONTROL_PLANE_ONLY,
        adapter=adapter,
        metrics_client=FakeMetricsClient(),
        host_sampler=FakeHostSampler(),
        strict_preflight=False,
    )

    result = asyncio.run(runner.run())

    assert len(result.workflows) == 20
    assert len(result.comparisons) == 20
    assert adapter.closed is True
    assert adapter.maximum["ordered"] == 1
    assert adapter.maximum["synchronized"] == 2
    synchronized = next(
        item
        for item in result.workflows
        if item.strategy == "shared" and item.stage == "synchronized_all_agents"
    )
    assert synchronized.agents == ("routine_agent", "strong_agent")
    assert synchronized.agent_order == ("routine_agent", "strong_agent")
    assert synchronized.max_parallel_model_calls == 2
    assert synchronized.model_calls == 2
    assert synchronized.input_tokens == 20
    assert synchronized.output_tokens == 10
    assert synchronized.total_tokens == 30
    assert synchronized.avg_latency_ms == 12.0
    assert synchronized.peak_latency_ms == 12.0
    assert synchronized.p50_latency_ms == 12.0
    assert synchronized.p95_latency_ms == 12.0
    assert synchronized.p99_latency_ms == 12.0
    assert synchronized.inference_wait_ms == 4.0
    assert synchronized.workflow_completed is True
    assert synchronized.host_telemetry_status == "control_plane_only"
    assert synchronized.gpu_telemetry_status == "unavailable"
    assert synchronized.gpu_util_avg_pct is None
    assert synchronized.cpu_avg_pct == 25.0
    assert synchronized.ram_avg_mb == 1024.0
    comparison = next(
        item for item in result.comparisons if item.strategy == "hybrid" and item.stage == "single"
    )
    assert comparison.quality_score == 1.0
    assert comparison.p95_latency_ratio_vs_shared == 1.0
    assert comparison.request_rps_ratio_vs_shared is not None
    assert comparison.gpu_util_ratio_vs_shared is None
    assert "control-plane-only" in comparison.notes
