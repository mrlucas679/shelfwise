from __future__ import annotations

import json
from pathlib import Path

import pytest

from shelfwise.training.collator import completion_for_row, messages_for_prompt
from shelfwise.training.config import load_training_config
from shelfwise.training.dataset import load_training_rows, parse_training_row, summarize_rows
from shelfwise.training.serving_check import run_serving_check

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def test_training_config_preserves_multimodal_lora_targets() -> None:
    config = load_training_config(CONFIG)

    assert config.model_name_or_path == "google/gemma-4-12B-it"
    assert config.max_seq_length == 2048
    assert {"patch_dense", "embedding_projection"}.issubset(config.lora.target_modules)
    assert config.modality.enable_image is True
    assert config.modality.enable_audio is True
    assert config.modality.enable_video is True


def test_multimodal_dataset_contract_covers_required_modalities() -> None:
    config = load_training_config(CONFIG)
    rows = load_training_rows(config.data.train_path, repo_root=ROOT, strict=True)
    summary = summarize_rows(rows)

    assert summary["row_count"] >= 4
    assert summary["modalities"]["image"] >= 1
    assert summary["modalities"]["audio"] >= 1
    assert summary["modalities"]["video"] >= 1
    assert summary["modalities"]["structured_json"] >= 1
    assert summary["unavailable_evidence"] == []


def test_prompt_preserves_placeholders_and_masks_assistant_as_completion() -> None:
    config = load_training_config(CONFIG)
    row = load_training_rows(config.data.train_path, repo_root=ROOT, strict=True)[1]

    prompt_text = "\n".join(message["content"] for message in messages_for_prompt(row))
    completion = completion_for_row(row)

    assert "<|image|>" in prompt_text
    assert "Do not sell damaged stock" not in prompt_text
    assert json.loads(completion)["risk_level"] == "high"


def test_missing_evidence_fails_in_strict_mode() -> None:
    raw = {
        "id": "bad-row",
        "case_type": "shipment_damage",
        "messages": [{"role": "user", "content": "x"}],
        "evidence": [
            {
                "type": "image",
                "path": "data/evidence/smoke/does-not-exist.png",
                "mime_type": "image/png",
                "description": "missing",
                "timestamp": "2026-07-09T00:00:00+02:00",
                "metadata": {},
            }
        ],
        "expected_output": {
            "summary": "x",
            "risk_level": "medium",
            "findings": [],
            "recommended_actions": [],
            "missing_information": [],
        },
    }

    with pytest.raises(ValueError, match="missing local file"):
        parse_training_row(raw, repo_root=ROOT, strict=True)


def test_serving_check_reads_adapter_metadata_fixture_without_model_load() -> None:
    """Always-running coverage of the serving-check logic against committed metadata.

    The artifact-gated test below silently skipped in every environment that lacks the
    283MB local adapter export (CI included), so this file's green status carried no
    serving-check coverage at all. The check only reads the small metadata JSONs when
    skip_model_load is set, so those files are committed as a fixture - the logic now
    runs everywhere, and the real-export test remains for machines that have it.
    """
    fixture_dir = ROOT / "tests" / "fixtures" / "adapter_metadata"

    summary = run_serving_check(CONFIG, adapter_path=fixture_dir, skip_model_load=True)

    assert summary["base_model"] == "google/gemma-4-12B-it"
    assert "patch_dense" in summary["target_modules"]
    assert summary["processor_class"] == "Gemma4UnifiedProcessor"


def test_serving_check_reads_exported_adapter_metadata_without_model_load() -> None:
    adapter_dir = ROOT / "shelfwise-gemma-final-adapter" / "final_adapter"
    if not adapter_dir.exists():
        pytest.skip(
            "real exported adapter not present locally - the committed metadata fixture "
            "test above still covers the serving-check logic"
        )

    summary = run_serving_check(CONFIG, adapter_path=adapter_dir, skip_model_load=True)

    assert summary["base_model"] == "google/gemma-4-12B-it"
    assert "patch_dense" in summary["target_modules"]
    assert summary["processor_class"] == "Gemma4UnifiedProcessor"
