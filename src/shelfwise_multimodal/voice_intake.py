from __future__ import annotations

from .contracts import Transcript, VoiceEventCandidate, VoiceIntent

_ACTION_WORDS = (
    "mark down",
    "markdown",
    "reorder",
    "discount",
    "switch supplier",
    "dispatch",
    "move stock",
)
_REPORT_WORDS = (
    "is warm",
    "is down",
    "out of",
    "broken",
    "leaking",
    "spoiled",
    "power",
    "generator",
)
_STATUS_WORDS = ("what", "status", "risk", "today", "how much", "show me")


def to_event_candidate(transcript: Transcript) -> VoiceEventCandidate:
    """Convert a transcript into a review-required event candidate."""
    intent = _classify(transcript.text)
    proposed = {
        VoiceIntent.REPORT_EVENT: "sensor_or_stock_report",
        VoiceIntent.REQUEST_ACTION: "action_request",
        VoiceIntent.ASK_STATUS: None,
        VoiceIntent.UNKNOWN: None,
    }[intent]
    confidence = (
        transcript.confidence
        if intent is not VoiceIntent.UNKNOWN
        else transcript.confidence * 0.5
    )
    return VoiceEventCandidate(
        intent=intent,
        proposed_event_type=proposed,
        summary=transcript.text,
        confidence=round(confidence, 4),
        requires_human_review=True,
        transcript=transcript,
    )


def _classify(text: str) -> VoiceIntent:
    """Classify common store-manager voice intents with transparent rules."""
    lowered = text.lower()
    if any(word in lowered for word in _ACTION_WORDS):
        return VoiceIntent.REQUEST_ACTION
    if any(word in lowered for word in _REPORT_WORDS):
        return VoiceIntent.REPORT_EVENT
    if any(word in lowered for word in _STATUS_WORDS):
        return VoiceIntent.ASK_STATUS
    return VoiceIntent.UNKNOWN
