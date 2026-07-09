from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .collator import apply_chat_template
from .config import DEFAULT_SPECIAL_TOKENS, load_training_config
from .runtime import special_token_report


def run_serving_check(
    config_path: str | Path,
    *,
    adapter_path: str | Path,
    skip_model_load: bool = False,
) -> dict[str, Any]:
    config = load_training_config(config_path)
    adapter_dir = Path(adapter_path)
    if not adapter_dir.exists():
        raise FileNotFoundError(f"adapter path not found: {adapter_dir}")
    adapter_config_path = adapter_dir / "adapter_config.json"
    tokenizer_config_path = adapter_dir / "tokenizer_config.json"
    if not adapter_config_path.exists():
        raise FileNotFoundError(f"adapter_config.json missing in {adapter_dir}")
    if not tokenizer_config_path.exists():
        raise FileNotFoundError(f"tokenizer_config.json missing in {adapter_dir}")
    adapter_config = json.loads(adapter_config_path.read_text(encoding="utf-8"))
    tokenizer_config = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
    summary: dict[str, Any] = {
        "adapter_path": str(adapter_dir),
        "base_model": adapter_config.get("base_model_name_or_path"),
        "target_modules": adapter_config.get("target_modules", []),
        "processor_class": tokenizer_config.get("processor_class"),
        "tokenizer_class": tokenizer_config.get("tokenizer_class"),
        "serving_model_name": config.serving.routine_model_name,
        "capability_summary": (
            "Text and placeholder multimodal prompts are checked. Full native audio/video "
            "understanding is not claimed unless the serving stack receives raw tensors."
        ),
    }
    missing = [
        token
        for token in DEFAULT_SPECIAL_TOKENS
        if token not in tokenizer_config.values()
        and token not in tokenizer_config.get("extra_special_tokens", [])
        and token not in tokenizer_config.get("model_specific_special_tokens", {}).values()
    ]
    summary["missing_special_tokens_from_config"] = missing
    if skip_model_load:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return summary

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(adapter_dir, trust_remote_code=True)
    summary["special_token_report"] = special_token_report(tokenizer)
    prompt = apply_chat_template(
        tokenizer,
        [
            {"role": "system", "content": "You are ShelfWise."},
            {
                "role": "user",
                "content": (
                    "Check this evidence prompt: <|image|> <|audio|> <|video|> "
                    "<|tool_call>{}<|tool_response>"
                ),
            },
        ],
    )
    tokenizer(prompt, add_special_tokens=False)
    summary["placeholder_prompt_tokenizes"] = True
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Check adapter/tokenizer serving compatibility")
    parser.add_argument("--config", required=True)
    parser.add_argument("--adapter-path", required=True)
    parser.add_argument("--skip-model-load", action="store_true")
    args = parser.parse_args()
    run_serving_check(
        args.config,
        adapter_path=args.adapter_path,
        skip_model_load=args.skip_model_load,
    )


if __name__ == "__main__":
    main()
