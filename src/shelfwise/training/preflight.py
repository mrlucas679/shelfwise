from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path
from typing import Any

from .collator import ShelfWiseDataCollator, build_tokenized_example, preview_batch
from .compatibility import validate_adapter_compatibility, write_adapter_manifest
from .config import MULTIMODAL_TARGETS, load_training_config
from .dataset import load_training_rows, summarize_rows
from .runtime import (
    enforce_tokenizer_length,
    lora_target_report,
    machine_info,
    package_versions,
    special_token_report,
    torch_device_info,
)


class PreflightFailure(RuntimeError):
    pass


def _print(title: str, payload: Any) -> None:
    print(f"\n[{title}]")
    print(payload)


def _load_model_stack(config: Any) -> tuple[Any, Any, Any]:
    try:
        from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer
    except ModuleNotFoundError as exc:
        raise PreflightFailure(
            "Missing transformers. Install training deps first: "
            "pip install transformers peft datasets accelerate tokenizers"
        ) from exc
    try:
        processor = AutoProcessor.from_pretrained(
            config.model_name_or_path,
            revision=config.model_revision,
            trust_remote_code=True,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            config.model_name_or_path,
            revision=config.model_revision,
            trust_remote_code=True,
        )
    except Exception as exc:
        raise PreflightFailure(
            f"Processor/tokenizer load failed for {config.model_name_or_path}: {exc}"
        ) from exc
    try:
        import torch

        dtype = torch.bfloat16 if config.bf16 else None
        model = AutoModelForCausalLM.from_pretrained(
            config.model_name_or_path,
            revision=config.model_revision,
            torch_dtype=dtype,
            device_map="auto",
            trust_remote_code=True,
        )
    except Exception as exc:
        raise PreflightFailure(
            f"Base model load failed for {config.model_name_or_path}: {exc}"
        ) from exc
    return processor, tokenizer, model


def run_preflight(
    config_path: str | Path,
    *,
    skip_model_load: bool = False,
    train_path: str | Path | None = None,
) -> int:
    repo_root = Path.cwd()
    config = load_training_config(config_path)
    _print("machine", machine_info())
    _print(
        "versions",
        package_versions(("torch", "transformers", "peft", "accelerate", "datasets", "tokenizers")),
    )
    try:
        import torch
    except ModuleNotFoundError as exc:
        raise PreflightFailure("torch is not installed in this environment") from exc
    device_info = torch_device_info(torch)
    _print("device", device_info)
    if not device_info["cuda_or_rocm_visible"]:
        raise PreflightFailure(
            "torch imported but no ROCm/CUDA device is visible; install the ROCm torch wheel and "
            "restart the kernel before training"
        )

    rows = load_training_rows(
        train_path or config.data.train_path,
        repo_root=repo_root,
        strict=config.data.strict_evidence,
    )
    summary = summarize_rows(rows)
    _print("dataset", summary)
    for modality, enabled in {
        "text": config.modality.enable_text,
        "image": config.modality.enable_image,
        "audio": config.modality.enable_audio,
        "video": config.modality.enable_video,
    }.items():
        if enabled and summary["modalities"].get(modality, 0) == 0:
            message = f"enabled modality has no training sample: {modality}"
            if config.modality.fail_when_enabled_modality_missing:
                raise PreflightFailure(message)
            print(f"WARNING: {message}")

    if skip_model_load:
        print("SKIPPED model/processor forward pass because --skip-model-load was set")
        return 0

    processor, tokenizer, model = _load_model_stack(config)
    enforce_tokenizer_length(tokenizer, config)
    if not getattr(tokenizer, "chat_template", None):
        raise PreflightFailure("tokenizer chat_template is missing")
    token_report = special_token_report(tokenizer)
    _print("special_tokens", token_report)
    missing_tokens = [token for token, item in token_report.items() if not item["present"]]
    if missing_tokens:
        raise PreflightFailure(f"tokenizer missing required special tokens: {missing_tokens}")

    target_report = lora_target_report(model, config.lora.target_modules)
    _print("lora_targets", target_report)
    missing_multimodal = MULTIMODAL_TARGETS.intersection(target_report["missing"])
    if missing_multimodal and not config.lora.allow_missing_multimodal_targets:
        raise PreflightFailure(
            "required multimodal LoRA targets missing from loaded model: "
            + ", ".join(sorted(missing_multimodal))
        )

    try:
        from peft import LoraConfig, get_peft_model
    except ModuleNotFoundError as exc:
        raise PreflightFailure("peft is missing; install peft before training") from exc
    peft_config = LoraConfig(
        task_type="CAUSAL_LM",
        r=config.lora.r,
        lora_alpha=config.lora.alpha,
        lora_dropout=config.lora.dropout,
        target_modules=list(config.lora.target_modules),
        revision=config.model_revision,
    )
    model = get_peft_model(model, peft_config)
    trainable = sum(
        parameter.numel() for parameter in model.parameters() if parameter.requires_grad
    )
    total = sum(parameter.numel() for parameter in model.parameters())
    _print("trainable_parameters", {"trainable": trainable, "total": total})

    examples = [
        build_tokenized_example(
            row,
            tokenizer=tokenizer,
            template_source=processor,
            max_seq_length=config.max_seq_length,
        )
        for row in rows[:1]
    ]
    collator = ShelfWiseDataCollator(tokenizer, pad_left=config.data.left_padding)
    batch = collator(examples)
    _print("collator_preview", preview_batch(examples, batch))
    outputs = model(**{key: value.to(model.device) for key, value in batch.items()})
    loss = float(outputs.loss.detach().cpu())
    if not math.isfinite(loss):
        raise PreflightFailure(f"one-step forward loss is not finite: {loss}")
    _print("one_step_forward", {"loss": loss})

    with tempfile.TemporaryDirectory() as tmp:
        adapter_dir = Path(tmp) / "adapter"
        model.save_pretrained(adapter_dir)
        tokenizer.save_pretrained(adapter_dir)
        write_adapter_manifest(adapter_dir, config)
        validate_adapter_compatibility(adapter_dir, config)
        from peft import PeftModel

        PeftModel.from_pretrained(model.base_model.model, adapter_dir)
    print("\npreflight ok: tiny adapter checkpoint saved and reloaded")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ShelfWise Gemma 4 multimodal preflight")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--skip-model-load",
        action="store_true",
        help="Validate config/dataset/device only; do not load Gemma or run forward pass.",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(run_preflight(args.config, skip_model_load=args.skip_model_load))
    except PreflightFailure as exc:
        print(f"PRECHECK FAILED: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
