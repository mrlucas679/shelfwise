from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from shelfwise.training.compatibility import (
    ADAPTER_MANIFEST_NAME,
    AdapterCompatibilityError,
    validate_adapter_compatibility,
)
from shelfwise.training.config import load_training_config, validate_training_config
from shelfwise.training.profiles import GEMMA4_PROFILES

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def _adapter(
    path: Path,
    *,
    base_model: str,
    revision: str | None = None,
    manifest_revision: str | None = None,
) -> Path:
    path.mkdir()
    (path / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": base_model,
                "revision": revision,
                "target_modules": ["q_proj"],
            }
        ),
        encoding="utf-8",
    )
    if manifest_revision is not None:
        profile = next(
            item for item in GEMMA4_PROFILES.values() if item.model_name_or_path == base_model
        )
        (path / ADAPTER_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile_name": profile.name,
                    "model_size": profile.size,
                    "base_model_name_or_path": base_model,
                    "base_model_revision": manifest_revision,
                }
            ),
            encoding="utf-8",
        )
    return path


def test_all_approved_gemma4_profiles_are_explicit() -> None:
    assert {profile.model_name_or_path for profile in GEMMA4_PROFILES.values()} == {
        "google/gemma-4-E2B-it",
        "google/gemma-4-E4B-it",
        "google/gemma-4-12B-it",
        "google/gemma-4-31B-it",
    }
    assert {profile.size for profile in GEMMA4_PROFILES.values()} == {
        "E2B",
        "E4B",
        "12B",
        "31B",
    }


def test_config_selects_exact_profile_revision_and_runtime_boundary() -> None:
    config = load_training_config(CONFIG)

    assert config.model_profile.model_name_or_path == config.model_name_or_path
    assert config.model_revision == "707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7"
    assert config.runtime.training_target == "w7900_jupyter"
    assert config.runtime.serving_target == "mi300x_endpoint"
    assert config.shakedown.train_examples == 120
    assert config.shakedown.eval_examples == 12


def test_adapter_compatibility_accepts_exact_base_and_default_revision(tmp_path: Path) -> None:
    config = load_training_config(CONFIG)
    adapter_dir = _adapter(tmp_path / "adapter", base_model=config.model_name_or_path)

    result = validate_adapter_compatibility(adapter_dir, config)

    assert result["compatible"] is True
    assert result["base_model_revision"] == "707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7"
    assert result["revision_source"] == "profile_default"


def test_cross_size_adapter_reuse_is_rejected(tmp_path: Path) -> None:
    config = load_training_config(CONFIG)
    adapter_dir = _adapter(
        tmp_path / "adapter",
        base_model="google/gemma-4-E4B-it",
    )

    with pytest.raises(AdapterCompatibilityError, match="cross-size adapter reuse"):
        validate_adapter_compatibility(adapter_dir, config)


def test_adapter_revision_must_match_selected_revision(tmp_path: Path) -> None:
    config = load_training_config(CONFIG)
    adapter_dir = _adapter(
        tmp_path / "adapter",
        base_model=config.model_name_or_path,
        manifest_revision="revision-a",
    )
    pinned_config = replace(config, model_revision="revision-b")

    with pytest.raises(AdapterCompatibilityError, match="revision mismatch"):
        validate_adapter_compatibility(adapter_dir, pinned_config)


def test_profile_and_model_id_cannot_drift() -> None:
    config = load_training_config(CONFIG)
    mismatched = replace(config, model_name_or_path="google/gemma-4-31B-it")

    with pytest.raises(ValueError, match="requires"):
        validate_training_config(mismatched)


def test_training_config_rejects_mutable_model_revision() -> None:
    """A rented-GPU run must record an immutable upstream base-model commit."""
    mutable = replace(load_training_config(CONFIG), model_revision="main")

    with pytest.raises(ValueError, match="immutable 40-character commit SHA"):
        validate_training_config(mutable)
