from __future__ import annotations

import math
from typing import Any

import httpx

from shelfwise_contracts import SourceRef

from .contracts import InteractionChannel, Transcript
from .settings import MultimodalSettings, load_settings


def _confidence_from_segments(segments: list[dict[str, Any]]) -> float:
    """Derive a bounded confidence from Whisper-style segment log probabilities."""
    log_probs = [segment["avg_logprob"] for segment in segments if "avg_logprob" in segment]
    if not log_probs:
        return 0.5
    mean_log_prob = sum(log_probs) / len(log_probs)
    return max(0.0, min(1.0, math.exp(mean_log_prob)))


async def transcribe(
    audio: bytes,
    *,
    audio_ref: SourceRef,
    language: str | None = None,
    channel: InteractionChannel = InteractionChannel.IN_APP_MIC,
    filename: str = "audio.wav",
    settings: MultimodalSettings | None = None,
) -> Transcript:
    """Call the OpenAI-compatible STT endpoint and return a reviewed transcript."""
    cfg = settings or load_settings()
    data = {"model": cfg.stt_model, "response_format": "verbose_json"}
    if language:
        data["language"] = language
    async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
        response = await client.post(
            f"{cfg.stt_base_url}/audio/transcriptions",
            data=data,
            files={"file": (filename, audio, "audio/wav")},
        )
        response.raise_for_status()
        body = response.json()
    return Transcript(
        text=str(body.get("text", "")).strip()[:2_000],
        language=str(body.get("language", language or "en"))[:16],
        confidence=_confidence_from_segments(body.get("segments", [])),
        channel=channel,
        audio_ref=audio_ref,
        duration_s=float(body.get("duration", 0.0)),
    )
