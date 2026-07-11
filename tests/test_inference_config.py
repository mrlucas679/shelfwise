from __future__ import annotations

from shelfwise_inference.config import InferenceConfig, ProviderKind, load_inference_config


def _config(base_url: str) -> InferenceConfig:
    return InferenceConfig(
        provider=ProviderKind.VLLM_MI300X,
        base_url=base_url,
        routine_model="shelfwise-routine",
        strong_model="shelfwise-strong",
        api_key="secret",
        api_key_present=True,
    )


def test_chat_completions_url_appends_v1_when_missing() -> None:
    assert (
        _config("http://localhost:8001").chat_completions_url()
        == "http://localhost:8001/v1/chat/completions"
    )


def test_chat_completions_url_trims_trailing_slash() -> None:
    assert (
        _config("http://localhost:8001/").chat_completions_url()
        == "http://localhost:8001/v1/chat/completions"
    )


def test_chat_completions_url_avoids_double_v1() -> None:
    assert (
        _config("https://api.fireworks.ai/inference/v1").chat_completions_url()
        == "https://api.fireworks.ai/inference/v1/chat/completions"
    )


def test_chat_completions_url_preserves_query_string() -> None:
    # A proxied notebook endpoint (e.g. AMD Developer Cloud's JupyterHub port proxy) needs an
    # auth token appended as a query string. The path must be inserted *before* that query,
    # not after it, or the request lands on the wrong route entirely.
    base = "https://radeon-global.anruicloud.com/instances/hf-289/proxy/8001?token=amd-oneclick"
    assert _config(base).chat_completions_url() == (
        "https://radeon-global.anruicloud.com/instances/hf-289/proxy/8001"
        "/v1/chat/completions?token=amd-oneclick"
    )


def test_chat_completions_url_preserves_query_string_with_v1_suffix() -> None:
    base = "https://host/proxy/8001/v1?token=amd-oneclick"
    assert (
        _config(base).chat_completions_url()
        == "https://host/proxy/8001/v1/chat/completions?token=amd-oneclick"
    )


def test_provider_detection_fireworks_vllm_offline(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://api.fireworks.ai/inference/v1")
    monkeypatch.setenv("LLM_API_KEY", "fw-key")
    assert load_inference_config().provider is ProviderKind.FIREWORKS

    monkeypatch.setenv("LLM_BASE_URL", "https://radeon-global.anruicloud.com/proxy/8001")
    assert load_inference_config().provider is ProviderKind.VLLM_MI300X

    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert load_inference_config().provider is ProviderKind.OFFLINE


def test_dual_model_endpoints_and_credentials_are_independent(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ROUTINE_BASE_URL", "https://routine.example/v1")
    monkeypatch.setenv("LLM_STRONG_BASE_URL", "https://strong.example/v1")
    monkeypatch.setenv("LLM_ROUTINE_API_KEY", "routine-key")
    monkeypatch.setenv("LLM_STRONG_API_KEY", "strong-key")
    monkeypatch.setenv("LLM_ROUTINE_MODEL", "google/gemma-4-E4B-it")
    monkeypatch.setenv("LLM_STRONG_MODEL", "google/gemma-4-31B-it")

    config = load_inference_config()

    assert config.base_url_for_agent("inventory") == "https://routine.example/v1"
    assert config.base_url_for_agent("executive") == "https://strong.example/v1"
    assert config.api_key_for_agent("inventory") == "routine-key"
    assert config.api_key_for_agent("critic") == "strong-key"
    assert config.dual_model_configured is True
    assert config.provider is ProviderKind.VLLM_MI300X
