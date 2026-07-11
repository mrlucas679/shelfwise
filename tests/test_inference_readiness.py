from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_inference import load_inference_config


def test_default_inference_readiness_is_safe_offline(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    client = TestClient(app)
    response = client.get("/inference/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_live_inference"] is False
    assert body["ready_for_amd_demo"] is False
    assert body["inference"]["provider"] == "offline"
    assert body["inference"]["timeout_seconds"] < 30


def test_vllm_mi300x_readiness_uses_openai_contract(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://203.0.113.10:8000")
    monkeypatch.setenv("LLM_API_KEY", "demo-key")
    monkeypatch.setenv("LLM_ROUTINE_MODEL", "google/gemma-4-E4B-it")
    monkeypatch.setenv("LLM_STRONG_MODEL", "google/gemma-4-31B-it")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "25")

    client = TestClient(app)
    response = client.get("/inference/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_live_inference"] is True
    assert body["ready_for_dual_model_inference"] is True
    assert body["ready_for_amd_demo"] is True
    assert body["inference"]["provider"] == "vllm_mi300x"
    assert body["inference"]["contract"] == "openai_chat_completions"
    assert body["inference"]["base_url_host"] == "203.0.113.10:8000"
    assert body["inference"]["api_key_present"] is True
    assert "demo-key" not in response.text


def test_timeout_is_clamped_under_submission_limit(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60")

    assert load_inference_config().timeout_seconds == 29


def test_submission_readiness_exposes_track_three_prescreen(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://203.0.113.10:8000")
    monkeypatch.setenv("LLM_API_KEY", "demo-key")
    monkeypatch.setenv("LLM_ROUTINE_MODEL", "google/gemma-4-E4B-it")
    monkeypatch.setenv("LLM_STRONG_MODEL", "google/gemma-4-31B-it")

    client = TestClient(app)
    response = client.get("/submission/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["track"] == "Track 3: Unicorn"
    assert body["ready_for_submission_prescreen"] is True
    assert body["checks"]["docker_image_required"] == "no"
    assert body["checks"]["amd_compute_usage"] == "ok"


def test_single_model_is_not_claimed_as_dual_model_submission_ready(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://203.0.113.10:8000")
    monkeypatch.setenv("LLM_API_KEY", "demo-key")
    monkeypatch.setenv("LLM_ROUTINE_MODEL", "google/gemma-4-E4B-it")
    monkeypatch.setenv("LLM_STRONG_MODEL", "google/gemma-4-E4B-it")

    body = TestClient(app).get("/submission/readiness").json()

    assert body["ready_for_submission_prescreen"] is False
    assert body["inference"]["ready_for_dual_model_inference"] is False
