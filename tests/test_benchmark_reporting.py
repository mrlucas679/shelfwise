from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from shelfwise_benchmark.models import (
    BenchmarkResult,
    RequestMetric,
    StrategyComparison,
)
from shelfwise_benchmark.reporting import write_benchmark_outputs


def _request() -> RequestMetric:
    return RequestMetric(
        run_id="run-1",
        strategy="shared",
        strategy_kind="shared",
        stage="single",
        repeat=1,
        workflow_id="workflow-1",
        agent="inventory",
        agent_order=1,
        parallel_group="inventory",
        tier="routine",
        endpoint="shared-primary",
        model="cloud-model",
        provider="vllm",
        started_at="2026-07-10T00:00:00+00:00",
        success=True,
        model_call=True,
        status_code=200,
        latency_ms=10.0,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        queue_time_ms=1.0,
        inference_time_ms=8.0,
        time_to_first_token_ms=3.0,
        generation_time_ms=5.0,
        mean_inter_token_latency_ms=1.0,
        tokens_per_second=50.0,
        quality_score=1.0,
        error_code="",
    )


def _comparison(strategy: str = "shared") -> StrategyComparison:
    return StrategyComparison(
        strategy=strategy,
        strategy_kind="shared",
        stage="single",
        concurrency=1,
        measurement_status="measured",
        workflows_started=1,
        workflows_completed=1,
        completion_rate=1.0,
        model_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        avg_latency_ms=10.0,
        peak_latency_ms=10.0,
        p50_latency_ms=10.0,
        p95_latency_ms=10.0,
        p99_latency_ms=10.0,
        request_rps=100.0,
        workflow_rps=10.0,
        quality_score=1.0,
        gpu_util_avg_pct=None,
        gpu_util_peak_pct=None,
        vram_peak_mb=None,
        cpu_avg_pct=20.0,
        ram_peak_mb=1000.0,
        queue_length_avg=0.0,
        queue_length_peak=0.0,
        idle_time_ms=None,
        inference_wait_ms=1.0,
        quality_delta_vs_shared=0.0,
        p95_latency_ratio_vs_shared=1.0,
        request_rps_ratio_vs_shared=1.0,
        workflow_rps_ratio_vs_shared=1.0,
        notes="CPU/RAM are control-plane-only; GPU/VRAM unavailable",
    )


def test_outputs_are_graph_ready_and_remove_stale_local_rows(tmp_path: Path) -> None:
    valid_request = _request()
    stale_request = replace(
        valid_request,
        strategy="direct_ollama_probe",
        endpoint="ollama",
        provider="ollama",
    )
    result = BenchmarkResult(
        run_id="run-1",
        created_at="2026-07-10T00:00:00+00:00",
        evidence_scope="control_plane_only",
        workflow_name="workflow",
        settings={
            "warmup_seconds": 1,
            "steady_seconds": 5,
            "repeats": 1,
            "stages": [
                {
                    "name": "single",
                    "workflow_concurrency": 1,
                    "synchronize_all_agents": False,
                }
            ],
        },
        requests=[valid_request, stale_request],
        comparisons=[_comparison(), _comparison("direct_ollama_probe")],
        warnings=["Local GPU values are not AMD cloud evidence."],
    )

    artifacts = write_benchmark_outputs(result, tmp_path)

    assert set(artifacts) == {
        "requests_csv",
        "workflows_csv",
        "windows_csv",
        "telemetry_csv",
        "comparisons_csv",
        "benchmark_json",
        "graph_series_json",
        "graph_series_csv",
        "report_markdown",
    }
    combined = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.values())
    assert "ollama" not in combined.casefold()
    assert "No winner is selected" in combined
    assert "Control-plane-only run" in combined
    assert "gpu_util_avg_pct" in artifacts["workflows_csv"].read_text(encoding="utf-8")
    graph = json.loads(artifacts["graph_series_json"].read_text(encoding="utf-8"))
    assert any(item["metric"] == "p95_latency_ms" for item in graph["series"])
    assert all("available" in item for item in graph["series"])
    benchmark = json.loads(artifacts["benchmark_json"].read_text(encoding="utf-8"))
    assert benchmark["excluded_stale_rows"] == 2
