from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MultimodalSettings:
    stt_base_url: str
    stt_model: str
    tts_base_url: str
    tts_model: str
    vlm_base_url: str
    vlm_model: str
    timeout_s: float
    enabled: bool


def load_settings() -> MultimodalSettings:
    """Load multimodal endpoints from environment on each call."""
    return MultimodalSettings(
        stt_base_url=os.getenv("STT_BASE_URL", "http://localhost:8000/v1"),
        stt_model=os.getenv("STT_MODEL", "openai/whisper-large-v3-turbo"),
        tts_base_url=os.getenv("TTS_BASE_URL", "http://localhost:8800"),
        tts_model=os.getenv("TTS_MODEL", "kokoro"),
        vlm_base_url=os.getenv("VLM_BASE_URL", "http://localhost:8000/v1"),
        vlm_model=os.getenv("VLM_MODEL", "Qwen/Qwen2-VL-7B-Instruct"),
        timeout_s=float(os.getenv("MULTIMODAL_TIMEOUT_S", "30")),
        enabled=os.getenv("MULTIMODAL_ENABLED", "false").strip().lower() == "true",
    )
