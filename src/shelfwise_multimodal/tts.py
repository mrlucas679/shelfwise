from __future__ import annotations

from xml.sax.saxutils import escape

import httpx

from .contracts import _FORBIDDEN_SPEECH, SpeechPurpose, SpokenReply, Tone
from .settings import MultimodalSettings, load_settings
from .speech_style import to_spoken

_PROSODY: dict[Tone, tuple[str, str]] = {
    Tone.CALM: ("medium", "medium"),
    Tone.URGENT: ("fast", "+2st"),
    Tone.WARM: ("medium", "+1st"),
}


class ApprovalSpeechForbidden(RuntimeError):
    """Raised when code tries to synthesize approval or authorization audio."""


async def synthesize(
    text: str,
    *,
    purpose: SpeechPurpose,
    tone: Tone = Tone.CALM,
    voice: str = "default",
    language: str = "en",
    settings: MultimodalSettings | None = None,
) -> tuple[SpokenReply, bytes]:
    """Synthesize spoken notification audio while blocking approval purposes."""
    if purpose in _FORBIDDEN_SPEECH:
        raise ApprovalSpeechForbidden(
            "Generated voice cannot stand in for human approval; keep approvals in HITL UI."
        )
    cfg = settings or load_settings()
    spoken = to_spoken(text)
    ssml = _ssml(spoken, tone)
    payload = {
        "model": cfg.tts_model,
        "input": spoken,
        "ssml": ssml,
        "voice": voice,
        "language": language,
    }
    async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
        response = await client.post(f"{cfg.tts_base_url}/tts", json=payload)
        response.raise_for_status()
        audio = response.content
    reply = SpokenReply(
        text=text,
        spoken_text=spoken,
        ssml=ssml,
        purpose=purpose,
        tone=tone,
        voice=voice,
        language=language,
    )
    return reply, audio


def _ssml(spoken: str, tone: Tone) -> str:
    """Wrap spoken text in simple tone-dependent SSML."""
    rate, pitch = _PROSODY[tone]
    body = escape(spoken).replace(" rand. ", ' rand.<break time="300ms"/> ')
    return f'<speak><prosody rate="{rate}" pitch="{pitch}">{body}</prosody></speak>'
