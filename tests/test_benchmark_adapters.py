from __future__ import annotations

import asyncio

import pytest

from shelfwise_benchmark.adapters import (
    endpoint_chat_url,
    parse_prometheus_metrics,
    parse_vllm_response,
)
from shelfwise_benchmark.models import EndpointSpec, EvidenceScope
from shelfwise_benchmark.telemetry import (
    AmdSmiSampler,
    parse_amd_smi_csv,
    parse_amd_smi_json,
)


def test_vllm_response_adapter_records_usage_and_per_request_metrics() -> None:
    body = {
        "choices": [{"message": {"content": '{"verdict": "approve"}'}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60},
        "metrics": {
            "time_to_first_token_ms": 20.5,
            "generation_time_ms": 40.5,
            "queue_time_ms": 3.25,
            "mean_itl_ms": 2.1,
            "tokens_per_second": 120.0,
        },
    }

    outcome = parse_vllm_response(body, 70.0, 200, ("verdict", "approve"))

    assert outcome.success is True
    assert outcome.prompt_tokens == 42
    assert outcome.completion_tokens == 18
    assert outcome.total_tokens == 60
    assert outcome.queue_time_ms == pytest.approx(3.25)
    assert outcome.inference_time_ms == pytest.approx(61.0)
    assert outcome.quality_score == 1.0


def test_prometheus_adapter_extracts_queue_and_histogram_samples() -> None:
    parsed = parse_prometheus_metrics(
        """
        # HELP vllm:num_requests_waiting waiting requests
        vllm:num_requests_waiting{model_name="routine"} 2
        vllm:num_requests_waiting{model_name="strong"} 1
        vllm:request_queue_time_seconds_sum 4.5
        vllm:request_queue_time_seconds_count 3
        """
    )

    assert parsed["vllm:num_requests_waiting"] == [2.0, 1.0]
    assert parsed["vllm:request_queue_time_seconds_sum"] == [4.5]


def test_amd_smi_json_and_csv_adapters_normalize_gpu_metrics() -> None:
    json_samples = parse_amd_smi_json(
        '{"gpu_0": {"GPU": 0, "GFX%": "75 %", "VRAM_USED": "4 GB"}}'
    )
    csv_samples = parse_amd_smi_csv("GPU,GFX%,VRAM_USED\n1,62 %,2048 MB\n")

    assert json_samples[0].device == "gpu_0"
    assert json_samples[0].gpu_util_pct == 75.0
    assert json_samples[0].vram_used_mb == 4096.0
    assert csv_samples[0].device == "gpu_1"
    assert csv_samples[0].gpu_util_pct == 62.0
    assert csv_samples[0].vram_used_mb == 2048.0


def test_amd_smi_is_disabled_for_control_plane_scope() -> None:
    sampler = AmdSmiSampler(binary="command-that-must-not-run")

    samples, reason = asyncio.run(sampler.sample(EvidenceScope.CONTROL_PLANE_ONLY))

    assert samples == []
    assert reason == "disabled_for_control_plane_only"


def test_chat_url_does_not_duplicate_v1_prefix() -> None:
    endpoint = EndpointSpec("e", "https://example.test/v1", "model")

    assert endpoint_chat_url(endpoint) == "https://example.test/v1/chat/completions"
