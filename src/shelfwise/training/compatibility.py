from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .profiles import profile_for_model

if TYPE_CHECKING:
    from .config import TrainingConfig

ADAPTER_MANIFEST_NAME = "shelfwise_adapter_manifest.json"


class AdapterCompatibilityError(RuntimeError):
    """Raised before an adapter is reused with an incompatible base model."""


@dataclass(frozen=True, slots=True)
class AdapterIdentity:
    base_model_name_or_path: str
    base_model_revision: str | None
    profile_name: str
    size: str
    revision_source: str


def write_adapter_manifest(adapter_dir: Path, config: TrainingConfig) -> Path:
    profile = config.model_profile
    payload = {
        "schema_version": 1,
        "adapter_format": "peft_lora",
        "profile_name": profile.name,
        "model_size": profile.size,
        "base_model_name_or_path": config.model_name_or_path,
        "base_model_revision": config.model_revision,
    }
    path = adapter_dir / ADAPTER_MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def validate_adapter_compatibility(
    adapter_dir: str | Path,
    config: TrainingConfig,
) -> dict[str, Any]:
    """Fail before loading an adapter trained for another model size or revision."""

    path = Path(adapter_dir)
    identity = read_adapter_identity(path)
    expected = config.model_profile
    if identity.size != expected.size:
        raise AdapterCompatibilityError(
            "cross-size adapter reuse is forbidden: "
            f"adapter is {identity.size} ({identity.base_model_name_or_path}) but "
            f"config selects {expected.size} ({config.model_name_or_path})"
        )
    if identity.base_model_name_or_path != config.model_name_or_path:
        raise AdapterCompatibilityError(
            "adapter base model mismatch: "
            f"adapter={identity.base_model_name_or_path!r}, "
            f"config={config.model_name_or_path!r}"
        )

    adapter_revision = identity.base_model_revision
    if adapter_revision is None:
        if config.model_revision != expected.default_revision:
            raise AdapterCompatibilityError(
                "adapter revision is absent, so it cannot be reused with pinned revision "
                f"{config.model_revision!r}"
            )
        adapter_revision = expected.default_revision
    if adapter_revision != config.model_revision:
        raise AdapterCompatibilityError(
            "adapter base revision mismatch: "
            f"adapter={adapter_revision!r}, config={config.model_revision!r}"
        )

    return {
        "compatible": True,
        "profile_name": identity.profile_name,
        "model_size": identity.size,
        "base_model_name_or_path": identity.base_model_name_or_path,
        "base_model_revision": adapter_revision,
        "revision_source": identity.revision_source,
    }


def read_adapter_identity(adapter_dir: str | Path) -> AdapterIdentity:
    path = Path(adapter_dir)
    adapter_config = _read_object(path / "adapter_config.json")
    manifest_path = path / ADAPTER_MANIFEST_NAME
    manifest = _read_object(manifest_path) if manifest_path.exists() else {}

    peft_base = _required_string(adapter_config, "base_model_name_or_path", path)
    manifest_base = manifest.get("base_model_name_or_path")
    if manifest_base is not None and manifest_base != peft_base:
        raise AdapterCompatibilityError(
            "adapter manifest conflicts with PEFT base model: "
            f"manifest={manifest_base!r}, adapter_config={peft_base!r}"
        )

    try:
        profile = profile_for_model(peft_base)
    except ValueError as exc:
        raise AdapterCompatibilityError(str(exc)) from exc

    manifest_profile = manifest.get("profile_name")
    if manifest_profile is not None and manifest_profile != profile.name:
        raise AdapterCompatibilityError(
            "adapter manifest profile does not match its base model: "
            f"manifest={manifest_profile!r}, derived={profile.name!r}"
        )
    manifest_size = manifest.get("model_size")
    if manifest_size is not None and manifest_size != profile.size:
        raise AdapterCompatibilityError(
            "adapter manifest size does not match its base model: "
            f"manifest={manifest_size!r}, derived={profile.size!r}"
        )

    peft_revision = _optional_string(adapter_config.get("revision"), "adapter revision")
    manifest_revision = _optional_string(
        manifest.get("base_model_revision"), "manifest base revision"
    )
    if peft_revision and manifest_revision and peft_revision != manifest_revision:
        raise AdapterCompatibilityError(
            "adapter manifest conflicts with PEFT revision: "
            f"manifest={manifest_revision!r}, adapter_config={peft_revision!r}"
        )
    revision = manifest_revision or peft_revision
    revision_source = "manifest" if manifest_revision else "peft_config"
    if revision is None:
        revision_source = "profile_default"

    return AdapterIdentity(
        base_model_name_or_path=peft_base,
        base_model_revision=revision,
        profile_name=profile.name,
        size=profile.size,
        revision_source=revision_source,
    )


def _read_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required adapter metadata missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterCompatibilityError(f"invalid adapter metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterCompatibilityError(f"adapter metadata must be a JSON object: {path}")
    return payload


def _required_string(payload: dict[str, Any], key: str, adapter_dir: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AdapterCompatibilityError(f"{key} missing in {adapter_dir / 'adapter_config.json'}")
    return value.strip()


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AdapterCompatibilityError(f"{label} must be a non-empty string or null")
    return value.strip()
