from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvidenceScope(StrEnum):
    """Describe where resource telemetry was collected."""

    CONTROL_PLANE_ONLY = "control_plane_only"
    CLOUD_INFERENCE_HOST = "cloud_inference_host"


class StrategyKind(StrEnum):
    """Describe the model-serving topology under test."""

    SHARED = "shared"
    REPLICATED = "replicated"
    PER_AGENT = "per_agent"
    HYBRID = "hybrid"


class TelemetryStatus(StrEnum):
    """State whether a metric is usable as architecture evidence."""

    MEASURED = "measured"
    CONTROL_PLANE_ONLY = "control_plane_only"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Define one benchmarked agent call and its quality check."""

    name: str
    order: int
    parallel_group: str
    tier: str
    prompt: str
    system_prompt: str = ""
    max_tokens: int = 128
    temperature: float = 0.0
    expected_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """Define the ordered agent workflow used by every strategy."""

    name: str
    agents: tuple[AgentSpec, ...]


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    """Define one OpenAI-compatible vLLM endpoint without storing secrets."""

    name: str
    base_url: str
    model: str
    api_key_env: str = ""
    provider: str = "vllm"
    chat_path: str = "/v1/chat/completions"
    metrics_url: str = ""
    timeout_seconds: float = 60.0

    @property
    def configured(self) -> bool:
        """Return whether the endpoint has enough non-secret routing data."""

        return bool(self.base_url and self.model)


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Map workflow agents or tiers to one or more endpoint replicas."""

    name: str
    kind: StrategyKind
    routes: dict[str, tuple[str, ...]]
    description: str = ""


@dataclass(frozen=True, slots=True)
class WorkloadStage:
    """Define workflow concurrency and synchronization behavior."""

    name: str
    workflow_concurrency: int
    synchronize_all_agents: bool = False


@dataclass(frozen=True, slots=True)
class RunSettings:
    """Control load stages, warmup, steady-state duration, and repeats."""

    peak_concurrency: int = 64
    synchronized_workflows: int = 1
    warmup_seconds: float = 5.0
    steady_seconds: float = 30.0
    repeats: int = 3
    telemetry_interval_seconds: float = 1.0
    max_workflows_per_window: int | None = None

    def stages(self) -> tuple[WorkloadStage, ...]:
        """Return the fixed ladder plus the configurable peak and worst case."""

        return (
            WorkloadStage("single", 1),
            WorkloadStage("moderate", 8),
            WorkloadStage("heavy", 32),
            WorkloadStage("peak", self.peak_concurrency),
            WorkloadStage(
                "synchronized_all_agents",
                self.synchronized_workflows,
                synchronize_all_agents=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Hold the complete workload and architecture configuration."""

    workflow: WorkflowSpec
    endpoints: dict[str, EndpointSpec]
    strategies: tuple[StrategySpec, ...]
    settings: RunSettings


@dataclass(slots=True)
class RequestOutcome:
    """Capture one provider response before benchmark metadata is attached."""

    success: bool
    model_call: bool
    status_code: int | None
    latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    queue_time_ms: float | None
    inference_time_ms: float | None
    time_to_first_token_ms: float | None
    generation_time_ms: float | None
    mean_inter_token_latency_ms: float | None
    tokens_per_second: float | None
    quality_score: float | None
    error_code: str = ""


@dataclass(slots=True)
class RequestMetric:
    """Record one agent request with provider timing and token metrics."""

    run_id: str
    strategy: str
    strategy_kind: str
    stage: str
    repeat: int
    workflow_id: str
    agent: str
    agent_order: int
    parallel_group: str
    tier: str
    endpoint: str
    model: str
    provider: str
    started_at: str
    success: bool
    model_call: bool
    status_code: int | None
    latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    queue_time_ms: float | None
    inference_time_ms: float | None
    time_to_first_token_ms: float | None
    generation_time_ms: float | None
    mean_inter_token_latency_ms: float | None
    tokens_per_second: float | None
    quality_score: float | None
    error_code: str


@dataclass(slots=True)
class TelemetrySample:
    """Record one server, host, or GPU telemetry observation."""

    run_id: str
    strategy: str
    stage: str
    repeat: int
    timestamp: str
    source: str
    scope: str
    target: str
    available: bool
    reason: str = ""
    gpu_util_pct: float | None = None
    vram_used_mb: float | None = None
    cpu_util_pct: float | None = None
    ram_used_mb: float | None = None
    queue_length: float | None = None
    running_requests: float | None = None
    queue_time_sum_seconds: float | None = None
    queue_time_count: float | None = None
    inference_time_sum_seconds: float | None = None
    inference_time_count: float | None = None


@dataclass(slots=True)
class WorkflowResult:
    """Aggregate every required metric for one workflow execution."""

    run_id: str
    strategy: str
    strategy_kind: str
    stage: str
    repeat: int
    workflow_id: str
    agents: tuple[str, ...]
    agent_order: tuple[str, ...]
    parallelism: str
    synchronized_all_agents: bool
    concurrency: int
    max_parallel_model_calls: int
    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    avg_latency_ms: float | None
    peak_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    rps: float | None
    gpu_util_avg_pct: float | None
    gpu_util_peak_pct: float | None
    vram_avg_mb: float | None
    vram_peak_mb: float | None
    cpu_avg_pct: float | None
    cpu_peak_pct: float | None
    ram_avg_mb: float | None
    ram_peak_mb: float | None
    queue_length_avg: float | None
    queue_length_peak: float | None
    idle_time_ms: float | None
    inference_wait_ms: float | None
    workflow_completion_ms: float
    workflow_completed: bool
    quality_score: float | None
    gpu_telemetry_status: str
    host_telemetry_status: str
    queue_telemetry_status: str
    inference_wait_status: str
    quality_status: str
    telemetry_scope: str
    error_count: int


@dataclass(slots=True)
class WindowResult:
    """Record elapsed time and completion counts for one steady window."""

    run_id: str
    strategy: str
    strategy_kind: str
    stage: str
    repeat: int
    concurrency: int
    configured_steady_seconds: float
    elapsed_ms: float
    workflows_started: int
    workflows_completed: int


@dataclass(slots=True)
class StrategyComparison:
    """Compare one strategy and load stage without declaring a winner."""

    strategy: str
    strategy_kind: str
    stage: str
    concurrency: int
    measurement_status: str
    workflows_started: int
    workflows_completed: int
    completion_rate: float | None
    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    avg_latency_ms: float | None
    peak_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    request_rps: float | None
    workflow_rps: float | None
    quality_score: float | None
    gpu_util_avg_pct: float | None
    gpu_util_peak_pct: float | None
    vram_peak_mb: float | None
    cpu_avg_pct: float | None
    ram_peak_mb: float | None
    queue_length_avg: float | None
    queue_length_peak: float | None
    idle_time_ms: float | None
    inference_wait_ms: float | None
    quality_delta_vs_shared: float | None = None
    p95_latency_ratio_vs_shared: float | None = None
    request_rps_ratio_vs_shared: float | None = None
    workflow_rps_ratio_vs_shared: float | None = None
    gpu_util_ratio_vs_shared: float | None = None
    vram_ratio_vs_shared: float | None = None
    notes: str = ""


@dataclass(slots=True)
class SkippedStrategy:
    """Record why a configured strategy was not measured."""

    strategy: str
    strategy_kind: str
    reason: str


@dataclass(slots=True)
class BenchmarkResult:
    """Hold raw evidence, summaries, warnings, and comparison rows."""

    run_id: str
    created_at: str
    evidence_scope: str
    workflow_name: str
    settings: dict[str, Any]
    requests: list[RequestMetric] = field(default_factory=list)
    workflows: list[WorkflowResult] = field(default_factory=list)
    windows: list[WindowResult] = field(default_factory=list)
    telemetry: list[TelemetrySample] = field(default_factory=list)
    comparisons: list[StrategyComparison] = field(default_factory=list)
    skipped_strategies: list[SkippedStrategy] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded_stale_rows: int = 0
