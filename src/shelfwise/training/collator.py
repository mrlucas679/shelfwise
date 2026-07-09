from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .dataset import EvidenceItem, TrainingRow

PLACEHOLDER_BY_TYPE = {
    "image": "<|image|>",
    "screenshot": "<|image|>",
    "audio": "<|audio|>",
    "video": "<|video|>",
}


@dataclass(frozen=True)
class CollatorPreview:
    decoded_prompt_preview: str
    evidence_types: list[str]
    input_shape: tuple[int, int]
    label_mask_ratio: float
    multimodal_tensors_present: bool
    truncation_count: int


def _evidence_line(item: EvidenceItem) -> str:
    placeholder = PLACEHOLDER_BY_TYPE.get(item.type, "")
    prefix = f"{placeholder} " if placeholder else ""
    fallback = f"; fallback={item.fallback}" if item.fallback else ""
    return (
        f"- {prefix}{item.type}: {item.description} "
        f"(path={item.path}; mime={item.mime_type}{fallback})"
    )


def messages_for_prompt(row: TrainingRow) -> list[dict[str, str]]:
    prompt_messages = [message for message in row.messages if message["role"] != "assistant"]
    evidence_block = "\n".join(_evidence_line(item) for item in row.evidence)
    if evidence_block:
        prompt_messages.append(
            {
                "role": "user",
                "content": (
                    "Supply-chain evidence attached for this case. Use it explicitly and "
                    "state missing information.\n"
                    f"{evidence_block}"
                ),
            }
        )
    return prompt_messages


def completion_for_row(row: TrainingRow) -> str:
    return json.dumps(row.expected_output, sort_keys=True, separators=(",", ":"))


def apply_chat_template(processor_or_tokenizer: Any, messages: list[dict[str, str]]) -> str:
    if hasattr(processor_or_tokenizer, "apply_chat_template"):
        return str(
            processor_or_tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
        )
    rendered = []
    for message in messages:
        rendered.append(f"{message['role'].upper()}: {message['content']}")
    rendered.append("ASSISTANT:")
    return "\n".join(rendered)


def _encode(tokenizer: Any, text: str) -> list[int]:
    encoded = tokenizer(text, add_special_tokens=False)
    input_ids = encoded["input_ids"] if isinstance(encoded, dict) else encoded.input_ids
    return list(input_ids)


def build_tokenized_example(
    row: TrainingRow,
    *,
    tokenizer: Any,
    template_source: Any | None = None,
    max_seq_length: int,
) -> dict[str, Any]:
    source = template_source or tokenizer
    prompt_text = apply_chat_template(source, messages_for_prompt(row))
    eos_token = getattr(tokenizer, "eos_token", None) or ""
    completion_text = completion_for_row(row) + eos_token
    prompt_ids = _encode(tokenizer, prompt_text)
    completion_ids = _encode(tokenizer, completion_text)
    input_ids = prompt_ids + completion_ids
    labels = [-100] * len(prompt_ids) + completion_ids
    truncated = 0
    if len(input_ids) > max_seq_length:
        truncated = len(input_ids) - max_seq_length
        input_ids = input_ids[-max_seq_length:]
        labels = labels[-max_seq_length:]
    return {
        "input_ids": input_ids,
        "labels": labels,
        "prompt_text": prompt_text,
        "evidence_types": sorted(row.evidence_types),
        "truncated_tokens": truncated,
    }


class ShelfWiseDataCollator:
    def __init__(self, tokenizer: Any, *, pad_left: bool = True) -> None:
        self.tokenizer = tokenizer
        self.pad_left = pad_left
        if pad_left and hasattr(tokenizer, "padding_side"):
            tokenizer.padding_side = "left"

    def __call__(self, batch: list[dict[str, Any]]) -> dict[str, Any]:
        import torch

        pad_token_id = getattr(self.tokenizer, "pad_token_id", None)
        if pad_token_id is None:
            pad_token_id = getattr(self.tokenizer, "eos_token_id", 0) or 0
        max_len = max(len(item["input_ids"]) for item in batch)
        input_ids: list[list[int]] = []
        labels: list[list[int]] = []
        attention_mask: list[list[int]] = []
        for item in batch:
            pad_len = max_len - len(item["input_ids"])
            if self.pad_left:
                input_ids.append([pad_token_id] * pad_len + item["input_ids"])
                labels.append([-100] * pad_len + item["labels"])
                attention_mask.append([0] * pad_len + [1] * len(item["input_ids"]))
            else:
                input_ids.append(item["input_ids"] + [pad_token_id] * pad_len)
                labels.append(item["labels"] + [-100] * pad_len)
                attention_mask.append([1] * len(item["input_ids"]) + [0] * pad_len)
        return {
            "input_ids": torch.tensor(input_ids),
            "labels": torch.tensor(labels),
            "attention_mask": torch.tensor(attention_mask),
        }


def preview_batch(batch: list[dict[str, Any]], collated: dict[str, Any]) -> CollatorPreview:
    total_labels = 0
    masked_labels = 0
    for item in batch:
        total_labels += len(item["labels"])
        masked_labels += sum(1 for label in item["labels"] if label == -100)
    shape = tuple(int(value) for value in collated["input_ids"].shape)
    return CollatorPreview(
        decoded_prompt_preview=batch[0]["prompt_text"][:500],
        evidence_types=batch[0]["evidence_types"],
        input_shape=(shape[0], shape[1]),
        label_mask_ratio=masked_labels / total_labels if total_labels else 0.0,
        multimodal_tensors_present=any(
            key not in {"input_ids", "labels", "attention_mask"} for key in collated
        ),
        truncation_count=sum(int(item["truncated_tokens"] > 0) for item in batch),
    )
