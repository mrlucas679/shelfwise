from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from shelfwise_backend.tenant import default_tenant_context, verify_bearer_token
from shelfwise_contracts import Event, EventSource, EventType, SourceRef

from .contracts import SpeechPurpose, Tone
from .settings import load_settings
from .stt import transcribe
from .tts import synthesize
from .vision import scan_image
from .voice_intake import to_event_candidate

_MAX_AUDIO_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_AUDIO_MAGIC: tuple[bytes, ...] = (
    b"\x1a\x45\xdf\xa3",
    b"OggS",
    b"RIFF",
    b"ID3",
    b"\xff\xfb",
    b"\xff\xf3",
)
_IMAGE_MAGIC: tuple[bytes, ...] = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
)


class VoiceOutBody(BaseModel):
    text: str = Field(min_length=1, max_length=1_200)
    tone: Tone = Tone.CALM


class BarcodeScanBody(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    location: str = Field(min_length=1, max_length=64)

    @field_validator("code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.replace("-", "").isalnum():
            raise ValueError("barcode must be alphanumeric")
        return cleaned


class ReceiptLineBody(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=10_000)
    unit_price: Decimal = Field(ge=0)


class ReceiptScanBody(BaseModel):
    receipt_id: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=64)
    lines: list[ReceiptLineBody] = Field(min_length=1, max_length=200)


def build_voice_router(*, api_key: str | None = None) -> APIRouter:
    """Build the optional voice router with upload and write-path guards."""

    async def guard(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
        expected = os.getenv("API_KEY", "") if api_key is None else api_key
        if expected and x_api_key != expected:
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    def enabled() -> None:
        if not load_settings().enabled:
            raise HTTPException(status_code=503, detail="voice disabled (MULTIMODAL_ENABLED=false)")

    router = APIRouter(prefix="/voice", dependencies=[Depends(guard)])

    @router.post("/in")
    async def voice_in(file: UploadFile) -> dict[str, object]:
        enabled()
        if not (file.content_type or "").startswith("audio/"):
            raise HTTPException(status_code=415, detail="expected an audio upload")
        audio = await file.read(_MAX_AUDIO_BYTES + 1)
        if len(audio) > _MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio too large")
        if not _looks_like_audio(audio[:12]):
            raise HTTPException(status_code=415, detail="payload is not a known audio container")
        transcript = await transcribe(
            audio,
            audio_ref=SourceRef.dataset("audio", file.filename or "mic"),
            filename=file.filename or "speech.webm",
        )
        candidate = to_event_candidate(transcript)
        return {
            "text": transcript.text,
            "intent": candidate.intent.value,
            "requires_human_review": candidate.requires_human_review,
        }

    @router.post("/out")
    async def voice_out(body: VoiceOutBody) -> Response:
        enabled()
        _reply, audio = await synthesize(
            body.text,
            purpose=SpeechPurpose.NOTIFY,
            tone=body.tone,
        )
        return Response(content=audio, media_type="audio/wav")

    return router


def build_scan_router(*, api_key: str | None = None) -> APIRouter:
    """Build the optional image-scan router with upload validation."""

    async def guard(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
        expected = os.getenv("API_KEY", "") if api_key is None else api_key
        if expected and x_api_key != expected:
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    router = APIRouter(prefix="/scan", dependencies=[Depends(guard)])

    @router.post("/barcode")
    async def scan_barcode(
        body: BarcodeScanBody,
        authorization: str | None = Header(default=None, alias="authorization"),
    ) -> dict[str, object]:
        candidate = _scan_event_candidate(body, tenant_id=_tenant_id(authorization))
        return {"candidate": candidate, "requires_human_review": True}

    @router.post("/receipt")
    async def scan_receipt(
        body: ReceiptScanBody,
        authorization: str | None = Header(default=None, alias="authorization"),
    ) -> dict[str, object]:
        tenant_id = _tenant_id(authorization)
        return {
            "candidates": [
                _receipt_line_candidate(body, line, tenant_id=tenant_id) for line in body.lines
            ],
            "requires_human_review": True,
        }

    @router.post("/image")
    async def scan_image_upload(file: UploadFile) -> dict[str, object]:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=415, detail="expected an image upload")
        image = await file.read(_MAX_IMAGE_BYTES + 1)
        if len(image) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image too large")
        if not _looks_like_image(image[:16]):
            raise HTTPException(status_code=415, detail="payload is not a known image container")
        evidence = await scan_image(
            image,
            image_ref=SourceRef.dataset("image", file.filename or "upload"),
        )
        return evidence.model_dump(mode="json")

    return router


def _looks_like_audio(head: bytes) -> bool:
    """Sniff known audio containers instead of trusting MIME or filename."""
    return head[4:8] == b"ftyp" or any(head.startswith(magic) for magic in _AUDIO_MAGIC)


def _looks_like_image(head: bytes) -> bool:
    """Sniff known image containers instead of trusting MIME or filename."""
    return head[8:12] == b"WEBP" or any(head.startswith(magic) for magic in _IMAGE_MAGIC)


def _scan_event_candidate(body: BarcodeScanBody, *, tenant_id: str) -> dict[str, object]:
    sku = _sku_from_code(body.code)
    event = Event(
        id=_event_id("barcode", tenant_id, body.location, body.code),
        type=EventType.SCAN,
        ts=datetime.now(UTC),
        actor=body.location,
        source=EventSource.SCANNER,
        tenant_id=tenant_id,
        payload={
            "sku": sku,
            "location": body.location,
            "barcode": body.code,
            "review_reason": "scanner candidates require human review before ingest",
        },
    )
    return {
        "event": event.to_dict(),
        "confidence": 0.95 if sku == body.code else 0.55,
        "source_ref": SourceRef.dataset("barcode_scan", body.code).to_dict(),
    }


def _receipt_line_candidate(
    body: ReceiptScanBody, line: ReceiptLineBody, *, tenant_id: str
) -> dict[str, object]:
    event = Event(
        id=_event_id(
            "receipt",
            tenant_id,
            body.receipt_id,
            line.sku,
            str(line.quantity),
            str(line.unit_price),
        ),
        type=EventType.SALE,
        ts=datetime.now(UTC),
        actor=body.location,
        source=EventSource.SCANNER,
        tenant_id=tenant_id,
        payload={
            "sku": line.sku,
            "location": body.location,
            "quantity": line.quantity,
            "unit_price": str(line.unit_price),
            "receipt_id": body.receipt_id,
            "review_reason": "receipt candidates require human review before ingest",
        },
    )
    return {
        "event": event.to_dict(),
        "confidence": 0.82,
        "source_ref": SourceRef.dataset("receipt_scan", body.receipt_id).to_dict(),
    }


def _sku_from_code(code: str) -> str:
    cleaned = code.strip()
    if cleaned.startswith("SKU-"):
        return cleaned.removeprefix("SKU-")
    return cleaned


def _tenant_id(authorization: str | None) -> str:
    """Resolve tenant identity from trusted auth, never from scan payload data."""
    if os.getenv("SHELFWISE_AUTH_MODE", "off").strip().lower() != "jwt":
        return default_tenant_context().tenant_id
    try:
        return verify_bearer_token(
            authorization,
            secret=os.getenv("TENANT_AUTH_SECRET", ""),
        ).tenant_id
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid tenant token") from exc


def _event_id(prefix: str, *parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"evt_{prefix}_{digest}"
