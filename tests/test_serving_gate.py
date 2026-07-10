from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shelfwise.training.config import DEFAULT_SPECIAL_TOKENS, load_training_config
from shelfwise.training.serving_check import (
    GENERATED_INFERENCE,
    METADATA_ONLY,
    run_serving_check,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def _adapter(path: Path) -> Path:
    config = load_training_config(CONFIG)
    path.mkdir()
    (path / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": config.model_name_or_path,
                "revision": config.model_revision,
                "target_modules": list(config.lora.target_modules),
            }
        ),
        encoding="utf-8",
    )
    (path / "tokenizer_config.json").write_text(
        json.dumps(
            {
                "processor_class": "Gemma4UnifiedProcessor",
                "tokenizer_class": "GemmaTokenizer",
                "extra_special_tokens": list(DEFAULT_SPECIAL_TOKENS),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_metadata_gate_never_claims_generated_inference(tmp_path: Path) -> None:
    adapter_dir = _adapter(tmp_path / "adapter")

    summary = run_serving_check(CONFIG, adapter_path=adapter_dir, mode=METADATA_ONLY)

    assert summary["gate"]["passed"] is True
    assert summary["gate"]["metadata_compatible"] is True
    assert summary["gate"]["generated_inference_observed"] is False
    assert summary["gate"]["deployment_ready"] is False
    assert summary["runtime_target"] == "mi300x_endpoint"


def test_generated_inference_gate_requires_real_endpoint_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adapter_dir = _adapter(tmp_path / "adapter")
    monkeypatch.setenv("SHELFWISE_MI300X_BASE_URL", "https://mi300x.example.test/v1")
    observed: dict[str, Any] = {}

    def transport(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        observed.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "model": "shelfwise",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The discrepancy is 9 crates. Hold receipt and reconcile it.",
                    }
                }
            ],
        }

    summary = run_serving_check(
        CONFIG,
        adapter_path=adapter_dir,
        mode=GENERATED_INFERENCE,
        endpoint_transport=transport,
    )

    assert observed["url"] == "https://mi300x.example.test/v1/chat/completions"
    assert summary["gate"]["generated_inference_observed"] is True
    assert summary["gate"]["deployment_ready"] is True
    assert summary["gate"]["passed"] is True


def test_prompt_echo_does_not_satisfy_generated_inference_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adapter_dir = _adapter(tmp_path / "adapter")
    monkeypatch.setenv("SHELFWISE_MI300X_BASE_URL", "https://mi300x.example.test")

    def echo_transport(
        _url: str,
        payload: dict[str, Any],
        _headers: dict[str, str],
        _timeout: float,
    ) -> dict[str, Any]:
        return {
            "model": "shelfwise",
            "choices": [{"message": {"content": payload["messages"][1]["content"]}}],
        }

    summary = run_serving_check(
        CONFIG,
        adapter_path=adapter_dir,
        mode=GENERATED_INFERENCE,
        endpoint_transport=echo_transport,
    )

    assert summary["gate"]["generated_inference_observed"] is False
    assert summary["gate"]["deployment_ready"] is False
    assert summary["gate"]["passed"] is False
