from __future__ import annotations

import urllib.error

import pytest

from shelfwise_inference.client import InferenceError, OpenAICompatibleInferenceClient
from shelfwise_inference.config import InferenceConfig, ProviderKind


def _config(*, api_key_present: bool = True) -> InferenceConfig:
    return InferenceConfig(
        provider=ProviderKind.FIREWORKS,
        base_url="https://api.fireworks.ai/inference/v1",
        routine_model="routine-model",
        strong_model="strong-model",
        api_key="test-key" if api_key_present else "",
        api_key_present=api_key_present,
    )


class _FakeHttpResponse:
    """Minimal stand-in for the context-manager object urlopen() returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_missing_api_key_raises_and_records_error_run() -> None:
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(
        _config(api_key_present=False), recorder=recorded.append
    )

    with pytest.raises(InferenceError, match="LLM_API_KEY is required"):
        client.complete(agent="executive", system="s", user="u")

    assert recorded[0]["status"] == "error"


def test_network_failure_raises_inference_error_and_records_it(monkeypatch) -> None:
    def fake_urlopen(request, timeout=30):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)

    with pytest.raises(InferenceError, match="Inference provider request failed"):
        client.complete(agent="executive", system="s", user="u")

    assert recorded[0]["status"] == "error"
    assert "connection refused" in recorded[0]["error_detail"]


def test_transient_network_failure_retries_once_within_timeout(monkeypatch) -> None:
    body = b'{"choices": [{"message": {"content": "ready"}}]}'
    calls = 0

    def fake_urlopen(request, timeout=30):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("connection reset")
        return _FakeHttpResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleInferenceClient(_config())

    result = client.chat_completions(
        agent="critic",
        messages=[{"role": "user", "content": "u"}],
        timeout_seconds=5,
    )

    assert result.content == "ready"
    assert calls == 2


def test_non_json_200_body_raises_inference_error_and_records_it(monkeypatch) -> None:
    """A malformed success response must not escape as a raw, unrecorded ValueError."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=30: _FakeHttpResponse(b"not json"),
    )
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)

    with pytest.raises(InferenceError, match="Inference provider request failed"):
        client.complete(agent="executive", system="s", user="u")

    assert recorded[0]["status"] == "error"


def test_missing_choices_in_a_valid_200_body_raises_and_records_it(monkeypatch) -> None:
    """A well-formed-but-wrong-shape response is a provider failure too, and must be recorded
    the same way as a network failure - not silently unrecorded."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=30: _FakeHttpResponse(b'{"unexpected": "shape"}'),
    )
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)

    with pytest.raises(InferenceError, match="Inference provider request failed"):
        client.complete(agent="executive", system="s", user="u")

    assert recorded[0]["status"] == "error"


def test_http_200_provider_error_sentinel_is_rejected_and_recorded(monkeypatch) -> None:
    body = b'{"choices": [{"message": {"content": "Internal Server Error"}}]}'
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=30: _FakeHttpResponse(body),
    )
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)

    with pytest.raises(InferenceError, match="Inference provider request failed"):
        client.complete(agent="executive", system="s", user="u")

    assert recorded[0]["status"] == "error"
    assert "error sentinel" in recorded[0]["error_detail"]


def test_http_200_provider_error_sentinel_retries_once(monkeypatch) -> None:
    error_body = b'{"choices": [{"message": {"content": "Internal Server Error"}}]}'
    ok_body = b'{"choices": [{"message": {"content": "ready"}}]}'
    bodies = iter((error_body, ok_body))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=30: _FakeHttpResponse(next(bodies)),
    )

    result = OpenAICompatibleInferenceClient(_config()).complete(
        agent="critic", system="s", user="u"
    )

    assert result.content == "ready"


def test_happy_path_returns_content_and_records_ok_run(monkeypatch) -> None:
    body = (
        b'{"choices": [{"message": {"content": "hello"}}], '
        b'"usage": {"prompt_tokens": 5, "completion_tokens": 2}}'
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=30: _FakeHttpResponse(body),
    )
    recorded: list[dict] = []
    client = OpenAICompatibleInferenceClient(_config(), recorder=recorded.append)

    result = client.complete(agent="executive", system="s", user="u")

    assert result.content == "hello"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert recorded[0]["status"] == "ok"
