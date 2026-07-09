from __future__ import annotations

import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_SPECIAL_TOKENS, TOKENIZER_PLACEHOLDER_MAX_LENGTH, TrainingConfig


def package_versions(names: tuple[str, ...]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    return versions


def git_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def timestamped_run_dir(base_dir: Path, run_name: str, *, timestamp: bool = True) -> Path:
    if timestamp:
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = base_dir / f"{run_name}-{suffix}"
    else:
        path = base_dir / run_name
    counter = 1
    candidate = path
    while candidate.exists():
        counter += 1
        candidate = path.with_name(f"{path.name}-{counter}")
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def machine_info() -> dict[str, str]:
    return {
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def torch_device_info(torch: Any) -> dict[str, Any]:
    visible = bool(torch.cuda.is_available())
    info: dict[str, Any] = {"cuda_or_rocm_visible": visible}
    if visible:
        props = torch.cuda.get_device_properties(0)
        info.update(
            {
                "name": torch.cuda.get_device_name(0),
                "vram_gb": round(props.total_memory / 1024**3, 2),
                "arch": getattr(props, "gcnArchName", "unknown"),
                "torch_version": getattr(torch, "__version__", "unknown"),
                "hip_version": getattr(getattr(torch, "version", None), "hip", None),
            }
        )
    return info


def special_token_report(tokenizer: Any) -> dict[str, Any]:
    report: dict[str, Any] = {}
    vocab = tokenizer.get_vocab() if hasattr(tokenizer, "get_vocab") else {}
    for token in DEFAULT_SPECIAL_TOKENS:
        token_id = None
        if hasattr(tokenizer, "convert_tokens_to_ids"):
            token_id = tokenizer.convert_tokens_to_ids(token)
        report[token] = {
            "present": token in vocab or token_id not in {None, -1},
            "id": token_id,
        }
    return report


def enforce_tokenizer_length(tokenizer: Any, config: TrainingConfig) -> None:
    model_max_length = int(getattr(tokenizer, "model_max_length", 0) or 0)
    if (
        config.safety.fail_on_tokenizer_placeholder_max_length
        and model_max_length >= TOKENIZER_PLACEHOLDER_MAX_LENGTH
        and not config.max_seq_length
    ):
        raise RuntimeError(
            "tokenizer has placeholder model_max_length; set explicit max_seq_length"
        )


def lora_target_report(model: Any, targets: tuple[str, ...]) -> dict[str, Any]:
    found: set[str] = set()
    suffixes = set(targets)
    for name, _module in model.named_modules():
        leaf = name.rsplit(".", 1)[-1]
        if leaf in suffixes:
            found.add(leaf)
    missing = sorted(set(targets).difference(found))
    return {"found": sorted(found), "missing": missing}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
