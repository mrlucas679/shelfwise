from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_contracts import SourceRef


class InteractionChannel(StrEnum):
    IN_APP_MIC = "in_app_mic"
    WHATSAPP_VOICE = "whatsapp_voice"
    PHONE_CALL = "phone_call"
    UPLOAD = "upload"


class SpeechPurpose(StrEnum):
    NOTIFY = "notify"
    EXPLAIN = "explain"
    NARRATE = "narrate"
    APPROVE = "approve"
    AUTHORIZE = "authorize"


_FORBIDDEN_SPEECH = {SpeechPurpose.APPROVE, SpeechPurpose.AUTHORIZE}


class Tone(StrEnum):
    CALM = "calm"
    URGENT = "urgent"
    WARM = "warm"


class VoiceIntent(StrEnum):
    ASK_STATUS = "ask_status"
    REPORT_EVENT = "report_event"
    REQUEST_ACTION = "request_action"
    UNKNOWN = "unknown"


class Transcript(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    language: str = "en"
    confidence: float = Field(ge=0.0, le=1.0)
    channel: InteractionChannel = InteractionChannel.IN_APP_MIC
    audio_ref: SourceRef
    duration_s: float = Field(default=0.0, ge=0.0)


class SpokenReply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    spoken_text: str = ""
    ssml: str | None = None
    purpose: SpeechPurpose
    tone: Tone = Tone.CALM
    audio_format: str = "wav"
    voice: str = "default"
    language: str = "en"


class VoiceEventCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: VoiceIntent
    proposed_event_type: str | None = None
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = True
    transcript: Transcript


class VisualEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    image_ref: SourceRef
    sku_candidate: str | None = None
    ocr_text: str = ""
    expiry_date: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = True
    model_id: str = "demo-fallback"
