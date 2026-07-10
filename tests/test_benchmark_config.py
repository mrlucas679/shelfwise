from __future__ import annotations

from pathlib import Path

import pytest

from shelfwise_benchmark import EvidenceScope, load_benchmark_config
from shelfwise_benchmark.routing import StrategyRouter, strategy_unavailable_reason

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "reports" / "templates" / "inference_architecture_benchmark.json"


def test_template_defines_all_strategies_and_workload_stages() -> None:
    config = load_benchmark_config(CONFIG_PATH, environ={})

    assert [strategy.kind.value for strategy in config.strategies] == [
        "shared",
        "replicated",
        "per_agent",
        "hybrid",
    ]
    stages = config.settings.stages()
    assert [(stage.name, stage.workflow_concurrency) for stage in stages] == [
        ("single", 1),
        ("moderate", 8),
        ("heavy", 32),
        ("peak", 64),
        ("synchronized_all_agents", 1),
    ]
    assert stages[-1].synchronize_all_agents is True
    assert len(config.workflow.agents) == 8


def test_replicated_router_uses_round_robin_endpoint_pool() -> None:
    environment = {
        "BENCH_REPLICA_A_BASE_URL": "https://a.example/v1",
        "BENCH_REPLICA_B_BASE_URL": "https://b.example/v1",
        "BENCH_REPLICATED_MODEL": "benchmark-model",
        "BENCHMARK_API_KEY": "not-exported",
    }
    config = load_benchmark_config(CONFIG_PATH, environ=environment)
    strategy = next(item for item in config.strategies if item.kind.value == "replicated")
    router = StrategyRouter(strategy, config.endpoints)
    agent = config.workflow.agents[0]

    assert [router.resolve(agent).name for _ in range(4)] == [
        "replicated_a",
        "replicated_b",
        "replicated_a",
        "replicated_b",
    ]


def test_control_plane_preflight_rejects_loopback_inference() -> None:
    environment = {
        "BENCH_SHARED_BASE_URL": "http://127.0.0.1:8000/v1",
        "BENCH_SHARED_MODEL": "benchmark-model",
        "BENCHMARK_API_KEY": "not-exported",
    }
    config = load_benchmark_config(CONFIG_PATH, environ=environment)
    strategy = next(item for item in config.strategies if item.kind.value == "shared")
    router = StrategyRouter(strategy, config.endpoints)

    reason = strategy_unavailable_reason(
        router,
        EvidenceScope.CONTROL_PLANE_ONLY,
        environ=environment,
    )

    assert "loopback" in reason
    assert "local inference evidence" in reason


def test_config_rejects_stale_local_provider_rows(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.json"
    config_path.write_text(
        """
        {
          "workflow": {"name": "w", "agents": [
            {"name": "a", "order": 1, "parallel_group": "a", "tier": "routine", "prompt": "x"}
          ]},
          "endpoints": {"legacy_ollama": {"provider": "ollama", "base_url": "http://x", "model": "x"}},
          "strategies": [{"name": "shared", "kind": "shared", "routes": {"default": ["legacy_ollama"]}}]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="provider must be vllm"):
        load_benchmark_config(config_path, environ={})
