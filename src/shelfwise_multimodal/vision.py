from __future__ import annotations

import base64
import json

import httpx

from shelfwise_contracts import SourceRef

from .contracts import VisualEvidence
from .settings import MultimodalSettings, load_settings

_SCAN_INSTRUCTION = (
    "You are a retail shelf scanner. Return ONLY JSON with keys: sku_candidate "
    "(string|null), ocr_text (string), expiry_date (YYYY-MM-DD|null), confidence (0..1)."
)


async def scan_image(
    image: bytes,
    *,
    image_ref: SourceRef,
    settings: MultimodalSettings | None = None,
) -> VisualEvidence:
    """Call the VLM scanner or return a reviewed offline fallback."""
    cfg = settings or load_settings()
    if not cfg.enabled:
        return _demo_evidence(image_ref)
    payload = {
        "model": cfg.vlm_model,
        "messages": [
            {"role": "system", "content": _SCAN_INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(image)},
                    }
                ],
            },
        ],
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
        response = await client.post(f"{cfg.vlm_base_url}/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
    # A hostile or malformed VLM response (bad envelope shape, non-JSON content, valid
    # JSON that isn't an object, a non-numeric confidence) must degrade to the reviewed
    # fallback, never crash the request.
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise TypeError("VLM response content is not a JSON object")
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (json.JSONDecodeError, KeyError, TypeError, IndexError, ValueError):
        return _demo_evidence(image_ref)
    return VisualEvidence(
        image_ref=image_ref,
        sku_candidate=parsed.get("sku_candidate"),
        ocr_text=str(parsed.get("ocr_text", "")),
        expiry_date=parsed.get("expiry_date"),
        confidence=confidence,
        requires_human_review=confidence < 0.85,
        model_id=cfg.vlm_model,
    )


def _demo_evidence(image_ref: SourceRef) -> VisualEvidence:
    """Return a deterministic reviewed evidence object when VLM is disabled."""
    return VisualEvidence(
        image_ref=image_ref,
        sku_candidate=None,
        ocr_text="",
        confidence=0.4,
        requires_human_review=True,
        model_id="demo-fallback",
    )


def _data_url(image: bytes) -> str:
    """Encode image bytes for OpenAI-compatible VLM requests."""
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
