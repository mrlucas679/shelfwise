from __future__ import annotations

import os
from pathlib import Path


def persistence_root() -> Path | None:
    """Return the configured durable root, or None when local defaults remain active."""
    raw = os.getenv("SHELFWISE_PERSIST_ROOT", "").strip()
    return Path(raw).expanduser() if raw else None


def durable_dir(env_name: str, default_relative: str) -> Path:
    """Resolve a state directory from an explicit variable or the durable root."""
    configured = os.getenv(env_name, "").strip()
    if configured:
        return Path(configured).expanduser()
    root = persistence_root()
    return root / default_relative if root is not None else Path(default_relative)


def durable_path(env_name: str, default_relative: str) -> Path:
    """Resolve a durable file path using the same environment policy as directories."""
    return durable_dir(env_name, default_relative)
