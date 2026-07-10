from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .adapters import (
    ControlPlaneAdapter,
    InferenceAdapter,
    VllmAdapter,
    VllmMetricsClient,
)
from .analysis import build_strategy_comparisons, summarize_workflow
from .models import (
    AgentSpec,
    BenchmarkConfig,
    BenchmarkResult,
    EvidenceScope,
    RequestMetric,
    RequestOutcome,
    SkippedStrategy,
    StrategySpec,
    WindowResult,
    WorkloadStage,
)
from .routing import StrategyRouter, strategy_unavailable_reason
from .telemetry import AmdSmiSampler, HostResourceSampler, TelemetryCollector


@dataclass(slots=True)
class _ExecutedWorkflow:
    """Hold raw request rows and elapsed time until telemetry is attached."""

    workflow_id: str
    requests: list[RequestMetric]
    completion_ms: float


class BenchmarkRunner:
    """Run every strategy through the workload ladder and aggregate evidence."""

    def __init__(
        self,
        config: BenchmarkConfig,
        *,
        scope: EvidenceScope,
        adapter: InferenceAdapter | None = None,
        metrics_client: VllmMetricsClient | None = None,
        host_sampler: HostResourceSampler | None = None,
        amd_smi_sampler: AmdSmiSampler | None = None,
        environ: Mapping[str, str] | None = None,
        plan_only: bool = False,
        strict_preflight: bool = True,
    ) -> None:
        """Store configuration and injectable provider/telemetry adapters."""

        self.config = config
        self.scope = scope
        self.plan_only = plan_only
        self.strict_preflight = strict_preflight
        self.environ = os.environ if environ is None else environ
        self.adapter = adapter or (ControlPlaneAdapter() if plan_only else VllmAdapter())
        self.metrics_client = metrics_client if metrics_client is not None else (
            None if plan_only else VllmMetricsClient()
        )
        self.host_sampler = host_sampler or HostResourceSampler()
        self.amd_smi_sampler = amd_smi_sampler or AmdSmiSampler()

    async def run(self) -> BenchmarkResult:
        """Execute configured strategies and return raw plus compared evidence."""

        self._validate_scope()
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        result = BenchmarkResult(
            run_id=run_id,
            created_at=datetime.now(UTC).isoformat(),
            evidence_scope=self.scope.value,
            workflow_name=self.config.workflow.name,
            settings=self._settings_dict(),
        )
        self._add_scope_warnings(result)
        try:
            for strategy in self.config.strategies:
                await self._run_strategy(result, strategy)
            result.comparisons = build_strategy_comparisons(
                self.config,
                result.workflows,
                result.requests,
                result.windows,
                result.telemetry,
                result.skipped_strategies,
                self.scope,
            )
            return result
        finally:
            await self.adapter.aclose()
            if self.metrics_client is not None:
                await self.metrics_client.aclose()

    async def _run_strategy(
        self,
        result: BenchmarkResult,
        strategy: StrategySpec,
    ) -> None:
        """Run every stage and repeat for one topology strategy."""

        router = StrategyRouter(strategy, self.config.endpoints)
        reason = ""
        if self.strict_preflight and not self.plan_only:
            reason = strategy_unavailable_reason(router, self.scope, environ=self.environ)
        if reason:
            result.skipped_strategies.append(
                SkippedStrategy(strategy.name, strategy.kind.value, reason)
            )
            result.warnings.append(f"{strategy.name}: {reason}")
            return
        for stage in self.config.settings.stages():
            for repeat in range(1, self.config.settings.repeats + 1):
                await self._run_repeat(result, strategy, router, stage, repeat)

    async def _run_repeat(
        self,
        result: BenchmarkResult,
        strategy: StrategySpec,
        router: StrategyRouter,
        stage: WorkloadStage,
        repeat: int,
    ) -> None:
        """Run warmup and one telemetry-backed steady window."""

        if self.config.settings.warmup_seconds > 0 and not self.plan_only:
            await self._run_window(
                result.run_id,
                strategy,
                router,
                stage,
                repeat,
                self.config.settings.warmup_seconds,
                collect=False,
            )
        endpoints = tuple(self.config.endpoints[name] for name in router.endpoint_names())
        collector = TelemetryCollector(
            run_id=result.run_id,
            strategy=strategy.name,
            stage=stage.name,
            repeat=repeat,
            scope=self.scope,
            endpoints=endpoints,
            interval_seconds=self.config.settings.telemetry_interval_seconds,
            metrics_client=self.metrics_client,
            host_sampler=self.host_sampler,
            amd_smi_sampler=self.amd_smi_sampler,
        )
        collector.start()
        started = time.perf_counter()
        executed = await self._run_window(
            result.run_id,
            strategy,
            router,
            stage,
            repeat,
            self.config.settings.steady_seconds,
            collect=True,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        telemetry = await collector.stop()
        self._append_repeat_results(
            result,
            strategy,
            stage,
            repeat,
            executed,
            telemetry,
            elapsed_ms,
        )

    async def _run_window(
        self,
        run_id: str,
        strategy: StrategySpec,
        router: StrategyRouter,
        stage: WorkloadStage,
        repeat: int,
        duration_seconds: float,
        *,
        collect: bool,
    ) -> list[_ExecutedWorkflow]:
        """Maintain fixed workflow concurrency for a bounded load window."""

        deadline = time.perf_counter() + duration_seconds
        maximum = self.config.settings.max_workflows_per_window
        if self.plan_only and maximum is None:
            maximum = stage.workflow_concurrency
        counter = 0
        lock = asyncio.Lock()
        completed: list[_ExecutedWorkflow] = []

        async def worker(worker_index: int) -> None:
            nonlocal counter
            while True:
                async with lock:
                    if (not self.plan_only and time.perf_counter() >= deadline) or (
                        maximum is not None and counter >= maximum
                    ):
                        return
                    workflow_index = counter
                    counter += 1
                workflow_id = (
                    f"{strategy.name}-{stage.name}-r{repeat}-"
                    f"w{worker_index}-{workflow_index:06d}"
                )
                execution = await self._execute_workflow(
                    run_id,
                    strategy,
                    router,
                    stage,
                    repeat,
                    workflow_id,
                )
                if collect:
                    completed.append(execution)

        workers = [asyncio.create_task(worker(index)) for index in range(stage.workflow_concurrency)]
        await asyncio.gather(*workers)
        if not self.plan_only:
            remaining = deadline - time.perf_counter()
            if remaining > 0:
                await asyncio.sleep(remaining)
        return completed

    async def _execute_workflow(
        self,
        run_id: str,
        strategy: StrategySpec,
        router: StrategyRouter,
        stage: WorkloadStage,
        repeat: int,
        workflow_id: str,
    ) -> _ExecutedWorkflow:
        """Execute ordered groups or the synchronized all-agent worst case."""

        started = time.perf_counter()
        requests: list[RequestMetric] = []
        if stage.synchronize_all_agents:
            requests.extend(
                await asyncio.gather(
                    *(
                        self._execute_agent(
                            run_id,
                            strategy,
                            router,
                            stage,
                            repeat,
                            workflow_id,
                            agent,
                        )
                        for agent in self.config.workflow.agents
                    )
                )
            )
        else:
            groups: dict[int, list[AgentSpec]] = defaultdict(list)
            for agent in self.config.workflow.agents:
                groups[agent.order].append(agent)
            for order in sorted(groups):
                requests.extend(
                    await asyncio.gather(
                        *(
                            self._execute_agent(
                                run_id,
                                strategy,
                                router,
                                stage,
                                repeat,
                                workflow_id,
                                agent,
                            )
                            for agent in groups[order]
                        )
                    )
                )
        requests.sort(key=lambda item: (item.agent_order, item.agent))
        return _ExecutedWorkflow(workflow_id, requests, (time.perf_counter() - started) * 1000)

    async def _execute_agent(
        self,
        run_id: str,
        strategy: StrategySpec,
        router: StrategyRouter,
        stage: WorkloadStage,
        repeat: int,
        workflow_id: str,
        agent: AgentSpec,
    ) -> RequestMetric:
        """Route one agent call and attach benchmark dimensions to its outcome."""

        endpoint = router.resolve(agent)
        started_at = datetime.now(UTC).isoformat()
        request_id = f"{workflow_id}-{agent.name}"
        try:
            outcome = await self.adapter.complete(endpoint, agent, request_id)
        except Exception:
            outcome = _adapter_failure()
        return _request_metric(
            run_id,
            strategy,
            stage,
            repeat,
            workflow_id,
            agent,
            endpoint.name,
            endpoint.model,
            endpoint.provider,
            started_at,
            outcome,
        )

    def _append_repeat_results(
        self,
        result: BenchmarkResult,
        strategy: StrategySpec,
        stage: WorkloadStage,
        repeat: int,
        executed: list[_ExecutedWorkflow],
        telemetry: list,
        elapsed_ms: float,
    ) -> None:
        """Attach shared telemetry and append workflow plus window rows."""

        result.telemetry.extend(telemetry)
        for item in executed:
            result.requests.extend(item.requests)
            result.workflows.append(
                summarize_workflow(
                    run_id=result.run_id,
                    strategy=strategy.name,
                    strategy_kind=strategy.kind.value,
                    stage=stage,
                    repeat=repeat,
                    workflow_id=item.workflow_id,
                    workflow=self.config.workflow,
                    requests=item.requests,
                    completion_ms=item.completion_ms,
                    telemetry=telemetry,
                    scope=self.scope,
                )
            )
        result.windows.append(
            WindowResult(
                result.run_id,
                strategy.name,
                strategy.kind.value,
                stage.name,
                repeat,
                stage.workflow_concurrency,
                self.config.settings.steady_seconds,
                elapsed_ms,
                len(executed),
                sum(item.workflow_completed for item in result.workflows[-len(executed) :])
                if executed
                else 0,
            )
        )

    def _settings_dict(self) -> dict:
        """Return JSON-ready settings including expanded workload stages."""

        values = asdict(self.config.settings)
        values["stages"] = [asdict(stage) for stage in self.config.settings.stages()]
        values["plan_only"] = self.plan_only
        return values

    def _add_scope_warnings(self, result: BenchmarkResult) -> None:
        """Add explicit evidence boundaries before any measurements run."""

        if self.scope is EvidenceScope.CONTROL_PLANE_ONLY:
            result.warnings.append(
                "Local host CPU/RAM are control-plane-only; local GPU/VRAM are disabled and "
                "must not be cited as AMD cloud inference evidence."
            )
        if self.plan_only:
            result.warnings.append(
                "Plan-only mode made zero model calls; latency, tokens, quality, and inference "
                "resource metrics are unavailable."
            )

    def _validate_scope(self) -> None:
        """Prevent Windows-local runs from claiming cloud-host GPU evidence."""

        if os.name == "nt" and self.scope is EvidenceScope.CLOUD_INFERENCE_HOST:
            raise ValueError(
                "cloud_inference_host scope is not allowed from Windows; use control_plane_only"
            )


def _request_metric(
    run_id: str,
    strategy: StrategySpec,
    stage: WorkloadStage,
    repeat: int,
    workflow_id: str,
    agent: AgentSpec,
    endpoint: str,
    model: str,
    provider: str,
    started_at: str,
    outcome: RequestOutcome,
) -> RequestMetric:
    """Convert a provider outcome into a fully labelled request row."""

    return RequestMetric(
        run_id,
        strategy.name,
        strategy.kind.value,
        stage.name,
        repeat,
        workflow_id,
        agent.name,
        agent.order,
        agent.parallel_group,
        agent.tier,
        endpoint,
        model,
        provider,
        started_at,
        outcome.success,
        outcome.model_call,
        outcome.status_code,
        outcome.latency_ms,
        outcome.prompt_tokens,
        outcome.completion_tokens,
        outcome.total_tokens,
        outcome.queue_time_ms,
        outcome.inference_time_ms,
        outcome.time_to_first_token_ms,
        outcome.generation_time_ms,
        outcome.mean_inter_token_latency_ms,
        outcome.tokens_per_second,
        outcome.quality_score,
        outcome.error_code,
    )


def _adapter_failure() -> RequestOutcome:
    """Return a safe failure when an injected adapter raises unexpectedly."""

    return RequestOutcome(
        success=False,
        model_call=True,
        status_code=None,
        latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        queue_time_ms=None,
        inference_time_ms=None,
        time_to_first_token_ms=None,
        generation_time_ms=None,
        mean_inter_token_latency_ms=None,
        tokens_per_second=None,
        quality_score=None,
        error_code="adapter_exception",
    )
