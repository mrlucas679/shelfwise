from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKENIZER_PLACEHOLDER_MAX_LENGTH = 10**20
MULTIMODAL_TARGETS = {"patch_dense", "embedding_projection"}
DEFAULT_SPECIAL_TOKENS = (
    "<pad>",
    "<bos>",
    "<eos>",
    "<|image|>",
    "<|audio|>",
    "<|video|>",
    "<|tool_call>",
    "<|tool_response>",
    "<|think|>",
    "<|channel>",
    "<|turn>",
)


@dataclass(frozen=True)
class LoraSettings:
    r: int = 16
    alpha: int = 32
    dropout: float = 0.05
    target_modules: tuple[str, ...] = (
        "patch_dense",
        "embedding_projection",
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    )
    allow_missing_multimodal_targets: bool = False


@dataclass(frozen=True)
class ModalitySettings:
    enable_text: bool = True
    enable_image: bool = True
    enable_audio: bool = True
    enable_video: bool = True
    raw_audio_video_fallback: str = "transcript_and_frames"
    fail_when_enabled_modality_missing: bool = False


@dataclass(frozen=True)
class DataSettings:
    train_path: Path = Path("data/training/shelfwise_multimodal_smoke.jsonl")
    eval_path: Path = Path("data/eval/shelfwise_multimodal_eval.jsonl")
    strict_evidence: bool = True
    video_frame_sample_count: int = 3
    mask_user_system_tokens: bool = True
    left_padding: bool = True


@dataclass(frozen=True)
class SafetySettings:
    fail_on_tokenizer_placeholder_max_length: bool = True
    fail_on_nan_loss: bool = True
    timestamp_output_dir: bool = True
    save_git_commit: bool = True


@dataclass(frozen=True)
class ServingSettings:
    adapter_name: str = "shelfwise"
    routine_model_name: str = "shelfwise"
    base_url: str = "http://127.0.0.1:8000"
    require_tool_call_parse: bool = False


@dataclass(frozen=True)
class TrainingConfig:
    model_name_or_path: str = "google/gemma-4-12B-it"
    output_dir: Path = Path("runs/gemma4-multimodal")
    run_name: str = "gemma4-mm"
    max_seq_length: int = 2048
    optional_max_seq_length: int = 4096
    bf16: bool = True
    fp16: bool = False
    gradient_checkpointing: bool = True
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 8
    learning_rate: float = 0.0002
    warmup_ratio: float = 0.03
    max_steps: int = 20
    max_train_hours: float = 8.0
    save_steps: int = 25
    eval_steps: int = 25
    logging_steps: int = 5
    resume_from_checkpoint: str | None = None
    lora: LoraSettings = field(default_factory=LoraSettings)
    modality: ModalitySettings = field(default_factory=ModalitySettings)
    data: DataSettings = field(default_factory=DataSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    serving: ServingSettings = field(default_factory=ServingSettings)


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ValueError(f"{path}:{line_no}: indentation must use two-space levels")
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise ValueError(f"{path}:{line_no}: expected key: value")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _parse_scalar(value)
            continue
        child: dict[str, Any] = {}
        parent[key] = child
        stack.append((indent, child))
    return root


def _path(value: Any) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def load_training_config(path: str | Path) -> TrainingConfig:
    """Load the constrained YAML config used by the training harness."""

    raw = _load_simple_yaml(Path(path))
    lora_raw = raw.get("lora", {})
    modality_raw = raw.get("modality", {})
    data_raw = raw.get("data", {})
    safety_raw = raw.get("safety", {})
    serving_raw = raw.get("serving", {})

    lora = LoraSettings(
        r=int(lora_raw.get("r", 16)),
        alpha=int(lora_raw.get("alpha", 32)),
        dropout=float(lora_raw.get("dropout", 0.05)),
        target_modules=tuple(str(item) for item in lora_raw.get("target_modules", []))
        or LoraSettings().target_modules,
        allow_missing_multimodal_targets=bool(
            lora_raw.get("allow_missing_multimodal_targets", False)
        ),
    )
    modality = ModalitySettings(
        enable_text=bool(modality_raw.get("enable_text", True)),
        enable_image=bool(modality_raw.get("enable_image", True)),
        enable_audio=bool(modality_raw.get("enable_audio", True)),
        enable_video=bool(modality_raw.get("enable_video", True)),
        raw_audio_video_fallback=str(
            modality_raw.get("raw_audio_video_fallback", "transcript_and_frames")
        ),
        fail_when_enabled_modality_missing=bool(
            modality_raw.get("fail_when_enabled_modality_missing", False)
        ),
    )
    data = DataSettings(
        train_path=_path(data_raw.get("train_path", DataSettings().train_path)),
        eval_path=_path(data_raw.get("eval_path", DataSettings().eval_path)),
        strict_evidence=bool(data_raw.get("strict_evidence", True)),
        video_frame_sample_count=int(data_raw.get("video_frame_sample_count", 3)),
        mask_user_system_tokens=bool(data_raw.get("mask_user_system_tokens", True)),
        left_padding=bool(data_raw.get("left_padding", True)),
    )
    safety = SafetySettings(
        fail_on_tokenizer_placeholder_max_length=bool(
            safety_raw.get("fail_on_tokenizer_placeholder_max_length", True)
        ),
        fail_on_nan_loss=bool(safety_raw.get("fail_on_nan_loss", True)),
        timestamp_output_dir=bool(safety_raw.get("timestamp_output_dir", True)),
        save_git_commit=bool(safety_raw.get("save_git_commit", True)),
    )
    serving = ServingSettings(
        adapter_name=str(serving_raw.get("adapter_name", "shelfwise")),
        routine_model_name=str(serving_raw.get("routine_model_name", "shelfwise")),
        base_url=str(serving_raw.get("base_url", "http://127.0.0.1:8000")),
        require_tool_call_parse=bool(serving_raw.get("require_tool_call_parse", False)),
    )

    config = TrainingConfig(
        model_name_or_path=str(raw.get("model_name_or_path", "google/gemma-4-12B-it")),
        output_dir=_path(raw.get("output_dir", "runs/gemma4-multimodal")),
        run_name=str(raw.get("run_name", "gemma4-mm")),
        max_seq_length=int(raw.get("max_seq_length", 2048)),
        optional_max_seq_length=int(raw.get("optional_max_seq_length", 4096)),
        bf16=bool(raw.get("bf16", True)),
        fp16=bool(raw.get("fp16", False)),
        gradient_checkpointing=bool(raw.get("gradient_checkpointing", True)),
        per_device_train_batch_size=int(raw.get("per_device_train_batch_size", 1)),
        gradient_accumulation_steps=int(raw.get("gradient_accumulation_steps", 8)),
        learning_rate=float(raw.get("learning_rate", 0.0002)),
        warmup_ratio=float(raw.get("warmup_ratio", 0.03)),
        max_steps=int(raw.get("max_steps", 20)),
        max_train_hours=float(raw.get("max_train_hours", 8)),
        save_steps=int(raw.get("save_steps", 25)),
        eval_steps=int(raw.get("eval_steps", 25)),
        logging_steps=int(raw.get("logging_steps", 5)),
        resume_from_checkpoint=raw.get("resume_from_checkpoint"),
        lora=lora,
        modality=modality,
        data=data,
        safety=safety,
        serving=serving,
    )
    validate_training_config(config)
    return config


def validate_training_config(config: TrainingConfig) -> None:
    if not config.max_seq_length or config.max_seq_length >= TOKENIZER_PLACEHOLDER_MAX_LENGTH:
        raise ValueError("max_seq_length must be explicit and must not use tokenizer placeholder")
    if config.max_seq_length not in {2048, 4096}:
        raise ValueError("max_seq_length must be 2048 by default or 4096 when explicitly selected")
    missing_multimodal = MULTIMODAL_TARGETS.difference(config.lora.target_modules)
    if missing_multimodal and not config.lora.allow_missing_multimodal_targets:
        missing = ", ".join(sorted(missing_multimodal))
        raise ValueError(f"required multimodal LoRA target modules missing from config: {missing}")
