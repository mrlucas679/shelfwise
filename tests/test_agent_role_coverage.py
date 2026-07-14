from __future__ import annotations

from shelfwise_eval.agent_role_coverage import _record_role_model_run
from shelfwise_mlops import InMemoryModelRunRegistry


def test_role_coverage_model_calls_enter_simulation_telemetry() -> None:
    registry = InMemoryModelRunRegistry()

    _record_role_model_run(
        registry,
        {
            "id": "mr_role_1",
            "tenant_id": "eval_tenant",
            "correlation_id": "corr_role_1",
            "agent": "inventory",
            "model": "google/gemma-4-E4B-it",
            "provider": "vllm_mi300x",
            "prompt_version": "v1",
            "schema_version": "v1",
            "input_tokens": 120,
            "output_tokens": 30,
            "latency_ms": 450,
        },
    )

    runs = registry.list(tenant_id="eval_tenant", data_domain="world_simulation")
    assert len(runs) == 1
    assert runs[0].agent == "inventory"
    assert runs[0].input_tokens + runs[0].output_tokens == 150
