from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .compatibility import validate_adapter_compatibility
from .config import DEFAULT_SPECIAL_TOKENS, load_training_config

METADATA_ONLY = "metadata_only"
GENERATED_INFERENCE = "generated_inference"
EndpointTransport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


class ServingGateFailure(RuntimeError):
    """Raised when the configured inference endpoint cannot be evaluated."""


def run_serving_check(
    config_path: str | Path,
    *,
    adapter_path: str | Path,
    skip_model_load: bool | None = None,
    mode: str | None = None,
    endpoint_transport: EndpointTransport | None = None,
) -> dict[str, Any]:
    config = load_training_config(config_path)
    adapter_dir = Path(adapter_path)
    tokenizer_config_path = adapter_dir / "tokenizer_config.json"
    if not tokenizer_config_path.exists():
        raise FileNotFoundError(f"tokenizer_config.json missing in {adapter_dir}")
    tokenizer_config = _read_object(tokenizer_config_path)
    compatibility = validate_adapter_compatibility(adapter_dir, config)
    selected_mode = _select_mode(config.serving.gate_mode, mode, skip_model_load)
    missing_tokens = _missing_special_tokens(tokenizer_config)
    metadata_compatible = not missing_tokens

    summary: dict[str, Any] = {
        "adapter_path": str(adapter_dir),
        "base_model": compatibility["base_model_name_or_path"],
        "base_model_revision": compatibility["base_model_revision"],
        "model_profile": compatibility["profile_name"],
        "model_size": compatibility["model_size"],
        "target_modules": _read_object(adapter_dir / "adapter_config.json").get(
            "target_modules", []
        ),
        "processor_class": tokenizer_config.get("processor_class"),
        "tokenizer_class": tokenizer_config.get("tokenizer_class"),
        "serving_model_name": config.serving.routine_model_name,
        "runtime_target": config.runtime.serving_target,
        "verification_level": selected_mode,
        "missing_special_tokens_from_config": missing_tokens,
        "compatibility": compatibility,
        "capability_summary": (
            "Metadata validation does not prove inference. Generated-inference mode probes the "
            "configured MI300X endpoint; it does not load or identify a local GPU."
        ),
    }
    if selected_mode == METADATA_ONLY:
        summary["gate"] = {
            "passed": metadata_compatible,
            "metadata_compatible": metadata_compatible,
            "generated_inference_observed": False,
            "deployment_ready": False,
            "scope": METADATA_ONLY,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return summary

    endpoint = _resolve_endpoint(config.serving.base_url, config.serving.base_url_env)
    probe_prompt = (
        "A receiving record expected 40 crates and observed 31. Calculate the discrepancy "
        "and state one safe next step. Do not repeat this prompt."
    )
    payload = {
        "model": config.serving.routine_model_name,
        "messages": [
            {"role": "system", "content": "You are the ShelfWise serving readiness probe."},
            {"role": "user", "content": probe_prompt},
        ],
        "temperature": 0,
        "max_tokens": config.serving.max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(config.serving.api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = endpoint_transport or _post_json
    try:
        response = transport(
            _chat_completions_url(endpoint),
            payload,
            headers,
            config.serving.request_timeout_seconds,
        )
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise ServingGateFailure(
            f"generated-inference endpoint probe failed for {config.runtime.serving_target}: {exc}"
        ) from exc

    generated, message = _generated_content(response)
    served_model = response.get("model")
    generated_observed = bool(generated.strip()) and not _is_prompt_echo(probe_prompt, generated)
    discrepancy_correct = bool(re.search(r"\b(?:9|nine)\b", generated.lower()))
    model_identity_matches = served_model == config.serving.routine_model_name
    tool_parse_observed = bool(message.get("tool_calls"))
    tool_requirement_met = not config.serving.require_tool_call_parse or tool_parse_observed
    passed = all(
        (
            metadata_compatible,
            generated_observed,
            discrepancy_correct,
            model_identity_matches,
            tool_requirement_met,
        )
    )
    summary.update(
        {
            "served_model": served_model,
            "generated_text": generated,
            "gate": {
                "passed": passed,
                "metadata_compatible": metadata_compatible,
                "generated_inference_observed": generated_observed,
                "probe_answer_correct": discrepancy_correct,
                "served_model_matches": model_identity_matches,
                "tool_parse_observed": tool_parse_observed,
                "deployment_ready": passed,
                "scope": GENERATED_INFERENCE,
            },
        }
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def _select_mode(config_mode: str, mode: str | None, skip_model_load: bool | None) -> str:
    if skip_model_load and mode == GENERATED_INFERENCE:
        raise ValueError("--skip-model-load cannot be combined with generated_inference mode")
    selected = METADATA_ONLY if skip_model_load else (mode or config_mode)
    if selected not in {METADATA_ONLY, GENERATED_INFERENCE}:
        raise ValueError(f"unsupported serving verification mode: {selected}")
    return selected


def _resolve_endpoint(base_url: str | None, env_name: str) -> str:
    endpoint = os.environ.get(env_name) or base_url
    if endpoint is None or not endpoint.strip():
        raise ServingGateFailure(
            f"generated_inference requires {env_name} or serving.base_url"
        )
    if not endpoint.startswith(("http://", "https://")):
        raise ServingGateFailure("serving endpoint must use http:// or https://")
    return endpoint.rstrip("/")


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/v1/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("serving endpoint response must be a JSON object")
    return parsed


def _generated_content(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ServingGateFailure("serving response has no completion choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ServingGateFailure("serving response choice has no message object")
    content = message.get("content")
    if not isinstance(content, str):
        raise ServingGateFailure("serving response message content is not text")
    return content.strip(), message


def _is_prompt_echo(prompt: str, generated: str) -> bool:
    def normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

    normalized_prompt = normalize(prompt)
    normalized_generated = normalize(generated)
    return not normalized_generated or normalized_generated == normalized_prompt


def _missing_special_tokens(tokenizer_config: dict[str, Any]) -> list[str]:
    configured: set[str] = set()
    for value in tokenizer_config.values():
        if isinstance(value, str):
            configured.add(value)
        elif isinstance(value, list):
            configured.update(item for item in value if isinstance(item, str))
        elif isinstance(value, dict):
            configured.update(item for item in value.values() if isinstance(item, str))
    return [token for token in DEFAULT_SPECIAL_TOKENS if token not in configured]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServingGateFailure(f"invalid serving metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServingGateFailure(f"serving metadata must be a JSON object: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Check adapter and endpoint serving readiness")
    parser.add_argument("--config", required=True)
    parser.add_argument("--adapter-path", required=True)
    parser.add_argument("--mode", choices=(METADATA_ONLY, GENERATED_INFERENCE))
    parser.add_argument(
        "--skip-model-load",
        action="store_true",
        help="Compatibility alias for metadata_only; no local model is loaded in either mode.",
    )
    args = parser.parse_args()
    try:
        summary = run_serving_check(
            args.config,
            adapter_path=args.adapter_path,
            skip_model_load=args.skip_model_load,
            mode=args.mode,
        )
    except ServingGateFailure as exc:
        print(f"SERVING GATE FAILED: {exc}")
        raise SystemExit(2) from exc
    if not summary["gate"]["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
