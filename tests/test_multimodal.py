from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shelfwise_backend.app import app as backend_app
from shelfwise_backend.deps import SESSION_COOKIE, write_limiter
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_contracts import SourceRef
from shelfwise_multimodal import (
    VOICE_SYSTEM_ADDENDUM,
    ApprovalSpeechForbidden,
    InteractionChannel,
    SpeechPurpose,
    Transcript,
    VoiceIntent,
    contracts,
    int_to_words,
    normalize_for_speech,
    router,
    scan_image,
    to_event_candidate,
    to_spoken,
)
from shelfwise_multimodal.router import build_scan_router, build_voice_router
from shelfwise_multimodal.settings import MultimodalSettings
from shelfwise_multimodal.stt import _confidence_from_segments
from shelfwise_multimodal.tts import synthesize


def _transcript(text: str, confidence: float = 0.9) -> Transcript:
    """Build a WhatsApp-style transcript fixture."""
    return Transcript(
        text=text,
        confidence=confidence,
        channel=InteractionChannel.WHATSAPP_VOICE,
        audio_ref=SourceRef.dataset("audio", "vn_1"),
    )


def _app(monkeypatch, *, enabled: bool, api_key: str = "") -> FastAPI:
    """Build a test app with the voice router mounted."""
    monkeypatch.setenv("MULTIMODAL_ENABLED", "true" if enabled else "false")
    app = FastAPI()
    app.include_router(build_voice_router(api_key=api_key))
    return app


def _scan_app(monkeypatch, *, enabled: bool, api_key: str = "") -> FastAPI:
    """Build a test app with the scan router mounted."""
    monkeypatch.setenv("MULTIMODAL_ENABLED", "true" if enabled else "false")
    app = FastAPI()
    app.include_router(build_scan_router(api_key=api_key))
    return app


def test_action_request_is_always_review_flagged():
    candidate = to_event_candidate(_transcript("mark down the yoghurt on aisle 7"))
    assert candidate.intent is VoiceIntent.REQUEST_ACTION
    assert candidate.requires_human_review is True
    assert candidate.proposed_event_type == "action_request"


def test_status_question_and_unknown_discounts_confidence():
    status_candidate = to_event_candidate(_transcript("what is at risk today?"))
    assert status_candidate.intent is VoiceIntent.ASK_STATUS
    unknown = to_event_candidate(_transcript("uhh hello", confidence=0.8))
    assert unknown.intent is VoiceIntent.UNKNOWN
    assert unknown.confidence < 0.8


def test_synthesizing_an_approval_is_forbidden():
    async def run() -> None:
        for purpose in (SpeechPurpose.APPROVE, SpeechPurpose.AUTHORIZE):
            try:
                await synthesize("approved", purpose=purpose)
            except ApprovalSpeechForbidden:
                continue
            raise AssertionError(f"{purpose} should be blocked")

    asyncio.run(run())


def test_confidence_is_monotonic_in_logprob():
    confident = _confidence_from_segments([{"avg_logprob": -0.05}, {"avg_logprob": -0.10}])
    shaky = _confidence_from_segments([{"avg_logprob": -1.5}, {"avg_logprob": -2.0}])
    assert 0.0 <= shaky < confident <= 1.0
    assert _confidence_from_segments([]) == 0.5


def test_demo_fallback_returns_reviewed_structured_evidence():
    evidence = asyncio.run(
        scan_image(
            b"\xff\xd8fake",
            image_ref=SourceRef.dataset("image", "shelf_1"),
            settings=MultimodalSettings(
                stt_base_url="",
                stt_model="",
                tts_base_url="",
                tts_model="",
                vlm_base_url="",
                vlm_model="",
                timeout_s=1,
                enabled=False,
            ),
        )
    )
    assert evidence.requires_human_review is True
    assert evidence.model_id == "demo-fallback"
    assert evidence.sku_candidate is None
    assert evidence.ocr_text == ""


class _FakeVlmResponse:
    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._body


def test_scan_image_falls_back_when_vlm_response_is_valid_json_but_not_an_object(monkeypatch):
    """A malformed/hostile VLM 200 response must degrade to the reviewed fallback, not crash."""

    async def fake_post(self, url, **kwargs):
        return _FakeVlmResponse({"choices": [{"message": {"content": "[]"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    evidence = asyncio.run(
        scan_image(
            b"\xff\xd8fake",
            image_ref=SourceRef.dataset("image", "shelf_1"),
            settings=MultimodalSettings(
                stt_base_url="",
                stt_model="",
                tts_base_url="",
                tts_model="",
                vlm_base_url="https://vlm.example",
                vlm_model="test-vlm",
                timeout_s=1,
                enabled=True,
            ),
        )
    )

    assert evidence.model_id == "demo-fallback"
    assert evidence.requires_human_review is True


def test_scan_image_falls_back_when_vlm_envelope_is_missing_choices(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return _FakeVlmResponse({"unexpected": "shape"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    evidence = asyncio.run(
        scan_image(
            b"\xff\xd8fake",
            image_ref=SourceRef.dataset("image", "shelf_1"),
            settings=MultimodalSettings(
                stt_base_url="",
                stt_model="",
                tts_base_url="",
                tts_model="",
                vlm_base_url="https://vlm.example",
                vlm_model="test-vlm",
                timeout_s=1,
                enabled=True,
            ),
        )
    )

    assert evidence.model_id == "demo-fallback"


def test_disabled_voice_returns_503_so_the_ui_degrades_to_text(monkeypatch):
    client = TestClient(_app(monkeypatch, enabled=False))
    response = client.post("/voice/in", files={"file": ("s.webm", b"x", "audio/webm")})
    assert response.status_code == 503


def test_voice_in_validates_type_size_and_reviewable_shape(monkeypatch):
    async def fake_transcribe(audio, **kwargs):
        _ = audio, kwargs
        return contracts.Transcript(
            text="mark down the yoghurt",
            confidence=0.9,
            audio_ref=SourceRef.dataset("audio", "mic"),
        )

    monkeypatch.setattr(router, "transcribe", fake_transcribe)
    client = TestClient(_app(monkeypatch, enabled=True))

    assert client.post("/voice/in", files={"file": ("x.png", b"x", "image/png")}).status_code == 415
    too_big = b"0" * (5 * 1024 * 1024 + 1)
    oversized = client.post(
        "/voice/in",
        files={"file": ("s.webm", too_big, "audio/webm")},
    )
    assert oversized.status_code == 413
    disguised = client.post(
        "/voice/in",
        files={"file": ("s.webm", b"MZ\x90\x00evil", "audio/webm")},
    )
    assert disguised.status_code == 415
    webm = b"\x1a\x45\xdf\xa3" + b"opus-frames"
    ok = client.post("/voice/in", files={"file": ("s.webm", webm, "audio/webm")}).json()
    assert ok == {
        "text": "mark down the yoghurt",
        "intent": "request_action",
        "requires_human_review": True,
    }


def test_voice_out_is_keyed_bounded_and_returns_audio(monkeypatch):
    async def fake_synthesize(text, **kwargs):
        _ = kwargs
        return (
            contracts.SpokenReply(text=text, purpose=contracts.SpeechPurpose.NOTIFY),
            b"RIFFfake",
        )

    monkeypatch.setattr(router, "synthesize", fake_synthesize)
    client = TestClient(_app(monkeypatch, enabled=True, api_key="k"))

    assert client.post("/voice/out", json={"text": "hi"}).status_code == 401
    response = client.post(
        "/voice/out",
        json={"text": "R12k at risk", "tone": "urgent"},
        headers={"x-api-key": "k"},
    )
    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")
    too_long = client.post(
        "/voice/out",
        json={"text": "x" * 2_000},
        headers={"x-api-key": "k"},
    )
    assert too_long.status_code == 422


def test_scan_image_validates_type_size_and_returns_reviewed_fallback(monkeypatch):
    client = TestClient(_scan_app(monkeypatch, enabled=False))

    wrong_type = client.post(
        "/scan/image",
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert wrong_type.status_code == 415
    too_big = b"0" * (8 * 1024 * 1024 + 1)
    oversized = client.post(
        "/scan/image",
        files={"file": ("shelf.jpg", too_big, "image/jpeg")},
    )
    assert oversized.status_code == 413
    disguised = client.post(
        "/scan/image",
        files={"file": ("shelf.jpg", b"MZ\x90\x00evil", "image/jpeg")},
    )
    assert disguised.status_code == 415

    ok = client.post(
        "/scan/image",
        files={"file": ("shelf.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
    )
    payload = ok.json()
    assert ok.status_code == 200
    assert payload["requires_human_review"] is True
    assert payload["model_id"] == "demo-fallback"
    assert payload["image_ref"]["ref"] == "image"
    assert payload["image_ref"]["locator"] == "shelf.jpg"


def test_barcode_scan_returns_review_required_scan_candidate(monkeypatch):
    client = TestClient(_scan_app(monkeypatch, enabled=False))

    response = client.post(
        "/scan/barcode",
        json={"code": "SKU-4011", "location": "store_12", "tenant_id": "sa_retail_demo"},
    )

    assert response.status_code == 200
    body = response.json()
    event = body["candidate"]["event"]
    assert body["requires_human_review"] is True
    assert event["type"] == "scan"
    assert event["payload"]["sku"] == "4011"
    assert event["payload"]["barcode"] == "SKU-4011"


def test_scan_router_accepts_the_browser_session_cookie(monkeypatch):
    secret = "scan-cookie-test-secret-at-least-32-characters"
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", secret)
    client = TestClient(_scan_app(monkeypatch, enabled=False))
    client.cookies.set(
        SESSION_COOKIE,
        encode_hs256_token(
            {
                "tenant_id": "tenant_browser",
                "user_id": "manager_browser",
                "role": "manager",
            },
            secret=secret,
        ),
    )

    response = client.post(
        "/scan/barcode",
        json={"code": "SKU-4011", "location": "store_12"},
    )

    assert response.status_code == 200
    assert response.json()["candidate"]["event"]["tenant_id"] == "tenant_browser"


def test_receipt_scan_returns_review_required_sale_candidates(monkeypatch):
    client = TestClient(_scan_app(monkeypatch, enabled=False))

    response = client.post(
        "/scan/receipt",
        json={
            "receipt_id": "r_1",
            "location": "store_12",
            "tenant_id": "sa_retail_demo",
            "lines": [{"sku": "4011", "quantity": 2, "unit_price": "30.00"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    event = body["candidates"][0]["event"]
    assert body["requires_human_review"] is True
    assert event["type"] == "sale"
    assert event["source"] == "scanner"
    assert event["payload"]["receipt_id"] == "r_1"
    assert event["payload"]["quantity"] == 2


def test_reviewed_scanner_candidate_enters_the_canonical_pipeline(monkeypatch):
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "off")
    candidate_client = TestClient(_scan_app(monkeypatch, enabled=False))
    event = candidate_client.post(
        "/scan/barcode",
        json={"code": "SKU-4011", "location": "store_12"},
    ).json()["candidate"]["event"]

    client = TestClient(backend_app)
    accepted = client.post(
        "/scan/candidates/confirm",
        json={"event": event, "review_note": "Matched the shelf label"},
    )
    duplicate = client.post(
        "/scan/candidates/confirm",
        json={"event": event, "review_note": "Matched the shelf label"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["event"]["payload"]["reviewed_by"]
    assert accepted.json()["event"]["payload"]["review_note"] == "Matched the shelf label"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"


def test_scan_confirmation_rejects_non_scanner_and_simulated_events(monkeypatch):
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "off")
    candidate_client = TestClient(_scan_app(monkeypatch, enabled=False))
    event = candidate_client.post(
        "/scan/barcode",
        json={"code": "SKU-4011", "location": "store_12"},
    ).json()["candidate"]["event"]
    client = TestClient(backend_app)

    wrong_source = client.post(
        "/scan/candidates/confirm",
        json={"event": {**event, "source": "manual"}},
    )
    wrong_domain = client.post(
        "/scan/candidates/confirm",
        json={"event": {**event, "data_domain": "world_simulation"}},
    )

    assert wrong_source.status_code == 422
    assert wrong_domain.status_code == 422


def test_scan_image_route_is_keyed_when_configured(monkeypatch):
    client = TestClient(_scan_app(monkeypatch, enabled=False, api_key="k"))

    assert (
        client.post(
            "/scan/image",
            files={"file": ("shelf.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
        ).status_code
        == 401
    )
    assert client.post("/scan/barcode", json={"code": "4011"}).status_code == 401


def test_enabled_backend_multimodal_routes_require_jwt_and_share_write_limit(monkeypatch):
    """Mounted upload routes must not bypass the backend auth/rate-limit perimeter."""
    secret = "multimodal-route-test-secret"
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", secret)
    monkeypatch.setenv("MULTIMODAL_ENABLED", "true")
    monkeypatch.delenv("API_KEY", raising=False)

    async def fake_transcribe(audio, **kwargs):
        _ = audio, kwargs
        return _transcript("check the yoghurt")

    async def fake_scan_image(image, **kwargs):
        _ = image, kwargs
        return contracts.VisualEvidence(
            image_ref=SourceRef.dataset("image", "shelf.jpg"),
            confidence=0.5,
        )

    monkeypatch.setattr(router, "transcribe", fake_transcribe)
    monkeypatch.setattr(router, "scan_image", fake_scan_image)
    manager = {
        "Authorization": "Bearer "
        + encode_hs256_token(
            {"tenant_id": "tenant_mm", "user_id": "manager_mm", "role": "manager"},
            secret=secret,
        )
    }
    analyst = {
        "Authorization": "Bearer "
        + encode_hs256_token(
            {"tenant_id": "tenant_mm", "user_id": "analyst_mm", "role": "analyst"},
            secret=secret,
        )
    }
    client = TestClient(backend_app)
    audio = {"file": ("voice.webm", b"\x1a\x45\xdf\xa3frames", "audio/webm")}
    try:
        write_limiter.configure(capacity=2, refill_per_s=0, max_keys=1024)
        assert client.post("/voice/in", files=audio).status_code == 401
        # The anonymous rejection reaches the shared limiter before JWT validation, so
        # reset the test bucket before asserting the authenticated caller's budget.
        write_limiter.clear()
        assert client.post("/voice/in", files=audio, headers=manager).status_code == 200
        assert client.post("/voice/in", files=audio, headers=manager).status_code == 200
        assert client.post("/voice/in", files=audio, headers=manager).status_code == 429

        write_limiter.configure(capacity=8, refill_per_s=0, max_keys=1024)
        image = {"file": ("shelf.jpg", b"\xff\xd8\xffimage", "image/jpeg")}
        assert client.post("/scan/image", files=image, headers=manager).status_code == 200
        assert (
            client.post(
                "/scan/barcode",
                json={"code": "SKU-4011", "location": "store_12"},
                headers=analyst,
            ).status_code
            == 403
        )
    finally:
        write_limiter.configure(capacity=240, refill_per_s=8.0, max_keys=1024)


def test_money_is_spoken_like_a_person():
    assert normalize_for_speech("R12,450 at risk") == (
        "twelve thousand four hundred and fifty rand at risk"
    )
    assert "twelve thousand rand" in normalize_for_speech("about R12k of yoghurt")
    assert "one rand and fifty cents" in normalize_for_speech("R1.50 unit cost")
    assert normalize_for_speech("R12 450 today").startswith(
        "twelve thousand four hundred and fifty rand"
    )


def test_sku_date_percent_and_markdown():
    assert "item four oh one one" in normalize_for_speech("SKU 4011 is slow")
    assert "the thirtieth of June" in normalize_for_speech("expires 2026-06-30")
    assert "twenty percent" in normalize_for_speech("mark down 20%")
    output = normalize_for_speech("**urgent** action")
    assert "*" not in output
    assert "urgent action" in output


def test_int_to_words_edges():
    assert int_to_words(0) == "zero"
    assert int_to_words(105) == "one hundred and five"
    assert int_to_words(1_000_000) == "one million"


def test_int_to_words_spells_digits_instead_of_crashing_beyond_the_scale_table():
    huge = 10**15
    assert int_to_words(huge) == "one " + " ".join(["oh"] * 15)


def test_normalize_for_speech_leaves_malformed_iso_dates_untouched_instead_of_crashing():
    assert normalize_for_speech("received 2025-13-40 batch") == "received 2025-13-40 batch"
    assert "the thirtieth of June" in normalize_for_speech("expires 2026-06-30")


def test_addendum_bans_the_llm_tells():
    lowered = VOICE_SYSTEM_ADDENDUM.lower()
    assert "as an ai" in lowered
    assert "no lists" in lowered
    assert "out loud" in lowered


def test_to_spoken_caps_length_and_speaks_numbers():
    long = "R12,450 at risk. Mark it down. Tell the team. Check fridge two. Then reorder."
    spoken = to_spoken(long, max_sentences=3)
    assert spoken.count(".") <= 3
    assert spoken.startswith("twelve thousand four hundred and fifty rand")
    assert "**" not in to_spoken("**bold** and `code`")
