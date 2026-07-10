from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profiles import Gemma4Profile, get_gemma4_profile

TOKENIZER_PLACEHOLDER_MAX_LENGTH = 10**20
MULTIMODAL_TARGETS = {"patch_dense", "embedding_projection"}
SHAKEDOWN_MIXTURES = {
    "supply_chain_reasoning",
    "multimodal_evidence",
    "simulation_incident",
    "report_action",
    "tool_call_structured",
}
DEFAULT_MIXTURE_WEIGHTS = {
    "supply_chain_reasoning": 0.30,
    "multimodal_evidence": 0.25,
    "simulation_incident": 0.20,
    "report_action": 0.15,
    "tool_call_structured": 0.10,
}
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
class EvaluationSettings:
    max_new_tokens: int = 384
    min_risk_accuracy: float = 0.75
    min_mean_actionability_score: float = 0.25
    min_mean_grounding_score: float = 0.25
    max_reference_echo_rate: float = 0.0


@dataclass(frozen=True)
class ShakedownSettings:
    smoke_steps: int = 20
    simulation_seed: int = 20260710
    train_examples: int = 120
    eval_examples: int = 12
    mixture_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_MIXTURE_WEIGHTS)
    )


@dataclass(frozen=True)
class RuntimeBoundarySettings:
    training_target: str = "w7900_jupyter"
    serving_target: str = "mi300x_endpoint"


@dataclass(frozen=True)
class ServingSettings:
    adapter_name: str = "shelfwise"
    routine_model_name: str = "shelfwise"
    base_url: str | None = None
    base_url_env: str = "SHELFWISE_MI300X_BASE_URL"
    api_key_env: str = "SHELFWISE_MI300X_API_KEY"
    gate_mode: str = "metadata_only"
    request_timeout_seconds: float = 60.0
    max_tokens: int = 128
    require_tool_call_parse: bool = False


@dataclass(frozen=True)
class TrainingConfig:
    profile_name: str = "gemma-4-12b-it"
    model_name_or_path: str = "google/gemma-4-12B-it"
    model_revision: str = "main"
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
    evaluation: EvaluationSettings = field(default_factory=EvaluationSettings)
    shakedown: ShakedownSettings = field(default_factory=ShakedownSettings)
    runtime: RuntimeBoundarySettings = field(default_factory=RuntimeBoundarySettings)
    serving: ServingSettings = field(default_factory=ServingSettings)

    @property
    def model_profile(self) -> Gemma4Profile:
        return get_gemma4_profile(self.profile_name)


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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def load_training_config(path: str | Path) -> TrainingConfig:
    """Load the constrained YAML config used by the training harness."""

    raw = _load_simple_yaml(Path(path))
    lora_raw = raw.get("lora", {})
    modality_raw = raw.get("modality", {})
    data_raw = raw.get("data", {})
    safety_raw = raw.get("safety", {})
    evaluation_raw = raw.get("evaluation", {})
    shakedown_raw = raw.get("shakedown", {})
    runtime_raw = raw.get("runtime", {})
    serving_raw = raw.get("serving", {})

    profile_name = str(raw.get("model_profile", "gemma-4-12b-it"))
    profile = get_gemma4_profile(profile_name)

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
    evaluation = EvaluationSettings(
        max_new_tokens=int(evaluation_raw.get("max_new_tokens", 384)),
        min_risk_accuracy=float(evaluation_raw.get("min_risk_accuracy", 0.75)),
        min_mean_actionability_score=float(
            evaluation_raw.get("min_mean_actionability_score", 0.25)
        ),
        min_mean_grounding_score=float(
            evaluation_raw.get("min_mean_grounding_score", 0.25)
        ),
        max_reference_echo_rate=float(
            evaluation_raw.get("max_reference_echo_rate", 0.0)
        ),
    )
    mixture_raw = shakedown_raw.get("mixture", DEFAULT_MIXTURE_WEIGHTS)
    if not isinstance(mixture_raw, dict):
        raise ValueError("shakedown.mixture must be a mapping of mixture name to weight")
    shakedown = ShakedownSettings(
        smoke_steps=int(shakedown_raw.get("smoke_steps", 20)),
        simulation_seed=int(shakedown_raw.get("simulation_seed", 20260710)),
        train_examples=int(shakedown_raw.get("train_examples", 120)),
        eval_examples=int(shakedown_raw.get("eval_examples", 12)),
        mixture_weights={str(key): float(value) for key, value in mixture_raw.items()},
    )
    runtime = RuntimeBoundarySettings(
        training_target=str(runtime_raw.get("training_target", "w7900_jupyter")),
        serving_target=str(runtime_raw.get("serving_target", "mi300x_endpoint")),
    )
    serving = ServingSettings(
        adapter_name=str(serving_raw.get("adapter_name", "shelfwise")),
        routine_model_name=str(serving_raw.get("routine_model_name", "shelfwise")),
        base_url=_optional_string(serving_raw.get("base_url")),
        base_url_env=str(
            serving_raw.get("base_url_env", "SHELFWISE_MI300X_BASE_URL")
        ),
        api_key_env=str(serving_raw.get("api_key_env", "SHELFWISE_MI300X_API_KEY")),
        gate_mode=str(serving_raw.get("gate_mode", "metadata_only")),
        request_timeout_seconds=float(
            serving_raw.get("request_timeout_seconds", 60.0)
        ),
        max_tokens=int(serving_raw.get("max_tokens", 128)),
        require_tool_call_parse=bool(serving_raw.get("require_tool_call_parse", False)),
    )

    config = TrainingConfig(
        profile_name=profile_name,
        model_name_or_path=str(raw.get("model_name_or_path", profile.model_name_or_path)),
        model_revision=str(raw.get("model_revision", profile.default_revision)),
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
        evaluation=evaluation,
        shakedown=shakedown,
        runtime=runtime,
        serving=serving,
    )
    validate_training_config(config)
    return config


def validate_training_config(config: TrainingConfig) -> None:
    profile = config.model_profile
    if config.model_name_or_path != profile.model_name_or_path:
        raise ValueError(
            f"model profile {profile.name!r} requires {profile.model_name_or_path!r}, "
            f"not {config.model_name_or_path!r}"
        )
    if not config.model_revision.strip():
        raise ValueError("model_revision must be explicit")
    if not config.max_seq_length or config.max_seq_length >= TOKENIZER_PLACEHOLDER_MAX_LENGTH:
        raise ValueError("max_seq_length must be explicit and must not use tokenizer placeholder")
    if config.max_seq_length not in {2048, 4096}:
        raise ValueError("max_seq_length must be 2048 by default or 4096 when explicitly selected")
    missing_multimodal = MULTIMODAL_TARGETS.difference(config.lora.target_modules)
    if missing_multimodal and not config.lora.allow_missing_multimodal_targets:
        missing = ", ".join(sorted(missing_multimodal))
        raise ValueError(f"required multimodal LoRA target modules missing from config: {missing}")
    if config.shakedown.smoke_steps <= 0:
        raise ValueError("shakedown.smoke_steps must be greater than zero")
    if config.shakedown.train_examples <= 0 or config.shakedown.eval_examples <= 0:
        raise ValueError("shakedown train_examples and eval_examples must be greater than zero")
    mixture_names = set(config.shakedown.mixture_weights)
    if mixture_names != SHAKEDOWN_MIXTURES:
        missing = sorted(SHAKEDOWN_MIXTURES.difference(mixture_names))
        unknown = sorted(mixture_names.difference(SHAKEDOWN_MIXTURES))
        raise ValueError(
            f"shakedown.mixture must contain the exact supported set; "
            f"missing={missing}, unknown={unknown}"
        )
    if any(weight <= 0 for weight in config.shakedown.mixture_weights.values()):
        raise ValueError("all shakedown mixture weights must be greater than zero")
    mixture_total = sum(config.shakedown.mixture_weights.values())
    if abs(mixture_total - 1.0) > 1e-6:
        raise ValueError(f"shakedown mixture weights must sum to 1.0, got {mixture_total}")
    for name, value in {
        "evaluation.min_risk_accuracy": config.evaluation.min_risk_accuracy,
        "evaluation.min_mean_actionability_score": (
            config.evaluation.min_mean_actionability_score
        ),
        "evaluation.min_mean_grounding_score": config.evaluation.min_mean_grounding_score,
        "evaluation.max_reference_echo_rate": config.evaluation.max_reference_echo_rate,
    }.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0.0 and 1.0")
    if config.evaluation.max_new_tokens <= 0:
        raise ValueError("evaluation.max_new_tokens must be greater than zero")
    if config.runtime.training_target != "w7900_jupyter":
        raise ValueError("runtime.training_target must remain w7900_jupyter")
    if config.runtime.serving_target != "mi300x_endpoint":
        raise ValueError("runtime.serving_target must remain mi300x_endpoint")
    if config.serving.gate_mode not in {"metadata_only", "generated_inference"}:
        raise ValueError(
            "serving.gate_mode must be metadata_only or generated_inference"
        )
    if config.serving.request_timeout_seconds <= 0 or config.serving.max_tokens <= 0:
        raise ValueError("serving timeout and max_tokens must be greater than zero")
