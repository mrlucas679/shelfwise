from shelfwise_backend.tools.model_runtime import architecture_from_inference_config
from shelfwise_inference.config import InferenceConfig, ProviderKind


def test_hybrid_runtime_routes_tiers_to_independent_endpoints() -> None:
    config = InferenceConfig(
        provider=ProviderKind.VLLM_MI300X,
        base_url="https://routine.example/v1",
        routine_model="google/gemma-4-E4B-it",
        strong_model="google/gemma-4-31B-it",
        api_key="common-key",
        api_key_present=True,
        routine_base_url="https://routine.example/v1",
        strong_base_url="https://strong.example/v1",
    )

    architecture = architecture_from_inference_config(config)

    assert architecture.target_for("inventory").endpoint == "https://routine.example/v1"
    assert architecture.target_for("inventory").model == "google/gemma-4-E4B-it"
    assert architecture.target_for("critic").endpoint == "https://strong.example/v1"
    assert architecture.target_for("critic").model == "google/gemma-4-31B-it"
