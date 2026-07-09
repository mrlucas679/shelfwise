from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path
from typing import Any

from .collator import ShelfWiseDataCollator, build_tokenized_example, preview_batch
from .config import load_training_config
from .dataset import load_training_rows, summarize_rows
from .runtime import git_commit, timestamped_run_dir, write_json


class TimeBudgetCallback:
    def __init__(self, max_seconds: float) -> None:
        from transformers import TrainerCallback

        class _Callback(TrainerCallback):
            def __init__(self, deadline: float) -> None:
                self.deadline = deadline

            def on_step_end(self, args: Any, state: Any, control: Any, **kwargs: Any) -> Any:
                if time.monotonic() >= self.deadline:
                    control.should_training_stop = True
                return control

        self.instance = _Callback(time.monotonic() + max_seconds)


def run_training(
    config_path: str | Path,
    *,
    run_name: str | None = None,
    max_steps: int | None = None,
    train_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    repo_root = Path.cwd()
    config = load_training_config(config_path)
    effective_run_name = run_name or config.run_name
    base_output_dir = Path(output_dir) if output_dir is not None else repo_root / config.output_dir
    output_dir = timestamped_run_dir(
        base_output_dir,
        effective_run_name,
        timestamp=config.safety.timestamp_output_dir,
    )
    shutil.copy2(config_path, output_dir / "train_config.yaml")
    if config.safety.save_git_commit:
        write_json(output_dir / "git.json", {"commit": git_commit(repo_root)})

    rows = load_training_rows(
        train_path or config.data.train_path,
        repo_root=repo_root,
        strict=config.data.strict_evidence,
    )
    write_json(output_dir / "dataset_summary.json", summarize_rows(rows))

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoProcessor,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
    )

    processor = AutoProcessor.from_pretrained(config.model_name_or_path, trust_remote_code=True)
    tokenizer = AutoTokenizer.from_pretrained(config.model_name_or_path, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        config.model_name_or_path,
        torch_dtype=torch.bfloat16 if config.bf16 else None,
        device_map="auto",
        trust_remote_code=True,
    )
    if config.gradient_checkpointing and hasattr(model, "gradient_checkpointing_enable"):
        model.gradient_checkpointing_enable()
    model = get_peft_model(
        model,
        LoraConfig(
            task_type="CAUSAL_LM",
            r=config.lora.r,
            lora_alpha=config.lora.alpha,
            lora_dropout=config.lora.dropout,
            target_modules=list(config.lora.target_modules),
        ),
    )
    examples = [
        build_tokenized_example(
            row,
            tokenizer=tokenizer,
            template_source=processor,
            max_seq_length=config.max_seq_length,
        )
        for row in rows
    ]
    collator = ShelfWiseDataCollator(tokenizer, pad_left=config.data.left_padding)
    preview = preview_batch(examples[:1], collator(examples[:1]))
    write_json(output_dir / "first_batch_preview.json", preview.__dict__)

    training_args = TrainingArguments(
        output_dir=str(output_dir / "checkpoints"),
        max_steps=max_steps if max_steps is not None else config.max_steps,
        per_device_train_batch_size=config.per_device_train_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        warmup_ratio=config.warmup_ratio,
        bf16=config.bf16,
        fp16=config.fp16,
        save_strategy="steps",
        save_steps=config.save_steps,
        eval_steps=config.eval_steps,
        logging_steps=config.logging_steps,
        report_to=[],
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=Dataset.from_list(examples),
        data_collator=collator,
        callbacks=[TimeBudgetCallback(config.max_train_hours * 3600).instance],
    )
    result = trainer.train(resume_from_checkpoint=config.resume_from_checkpoint)
    metrics = dict(result.metrics)
    write_json(output_dir / "train_metrics.json", metrics)
    if config.safety.fail_on_nan_loss:
        train_loss = metrics.get("train_loss")
        if train_loss is not None and str(train_loss).lower() in {"nan", "inf", "-inf"}:
            raise RuntimeError(f"training loss is not finite: {train_loss}")
    adapter_dir = output_dir / "final_adapter"
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    if hasattr(processor, "save_pretrained"):
        processor.save_pretrained(adapter_dir)
    print(f"training complete: {output_dir}")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="ShelfWise Gemma 4 multimodal training")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run_name")
    parser.add_argument("--max_steps", type=int)
    args = parser.parse_args()
    run_training(args.config, run_name=args.run_name, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
