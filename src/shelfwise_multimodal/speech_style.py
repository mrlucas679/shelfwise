from __future__ import annotations

import re

from .text_normalize import normalize_for_speech

VOICE_SYSTEM_ADDENDUM = """\
You are speaking OUT LOUD to a busy shop manager, not writing a report.
Lead with the risk and the one action. Use short sentences and contractions.
Say numbers like a person, not like raw screen text. No lists, no bullets, no markdown.
Never say "As an AI" or "Based on the data". Three sentences max unless they ask for more.
You never approve anything by voice. If they say "do it", ask them to confirm on screen.
"""


def to_spoken(text: str, *, max_sentences: int = 4) -> str:
    """Normalize and cap text so TTS receives a short spoken reply."""
    spoken = normalize_for_speech(text)
    sentences = _split_sentences(spoken)
    if len(sentences) > max_sentences:
        return " ".join(sentences[:max_sentences])
    return spoken


def _split_sentences(text: str) -> list[str]:
    """Split a reply into simple sentence chunks for spoken delivery."""
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]
