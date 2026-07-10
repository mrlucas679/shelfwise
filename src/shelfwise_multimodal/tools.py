from __future__ import annotations

from shelfwise_contracts import SourceRef

from .contracts import (
    SpeechPurpose,
    SpokenReply,
    Tone,
    Transcript,
    VisualEvidence,
    VoiceEventCandidate,
)
from .stt import transcribe
from .tts import synthesize
from .vision import scan_image
from .voice_intake import to_event_candidate


async def voice_in_tool(
    audio: bytes,
    *,
    audio_ref: SourceRef,
    language: str | None = None,
) -> VoiceEventCandidate:
    """Transcribe audio and return a review-required candidate."""
    transcript: Transcript = await transcribe(audio, audio_ref=audio_ref, language=language)
    return to_event_candidate(transcript)


async def voice_out_tool(
    text: str,
    *,
    purpose: SpeechPurpose = SpeechPurpose.NOTIFY,
    tone: Tone = Tone.CALM,
) -> tuple[SpokenReply, bytes]:
    """Synthesize a spoken reply for backend or MCP callers."""
    return await synthesize(text, purpose=purpose, tone=tone)


async def scan_tool(image: bytes, *, image_ref: SourceRef) -> VisualEvidence:
    """Scan an image into structured visual evidence."""
    return await scan_image(image, image_ref=image_ref)
