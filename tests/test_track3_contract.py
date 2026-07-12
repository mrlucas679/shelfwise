from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import _request_timeout_seconds, app
from shelfwise_backend.chat import ensure_english_response
from shelfwise_backend.chat_store import ChatConversationStore
from shelfwise_inference.config import _timeout_seconds
from shelfwise_inference.orchestration import AgentOrchestrationError, _ensure_english_payload


def test_track3_english_guard_rejects_non_latin_model_output() -> None:
    with pytest.raises(Exception, match="non-English"):
        ensure_english_response("这是一个中文回答")


def test_track3_english_guard_accepts_operational_english() -> None:
    assert ensure_english_response("Stock is below the reorder point.") == (
        "Stock is below the reorder point."
    )


def test_track3_agent_payload_english_guard_checks_nested_response_fields() -> None:
    with pytest.raises(AgentOrchestrationError, match="non-English"):
        _ensure_english_payload({"answer": "これは日本語です"})


def test_track3_replay_is_message_id_scoped_not_question_cached() -> None:
    store = ChatConversationStore()
    store.append_exchange(
        tenant_id="tenant",
        user_id="user",
        conversation_id="conversation",
        message_id="message-1",
        question="What is the stock position?",
        answer="Answer one",
        metadata={},
    )

    assert (
        store.answer_for_message(
            tenant_id="tenant",
            user_id="user",
            conversation_id="conversation",
            message_id="message-1",
        )
        is not None
    )
    assert (
        store.answer_for_message(
            tenant_id="tenant",
            user_id="user",
            conversation_id="conversation",
            message_id="message-2",
        )
        is None
    )


def test_track3_production_chat_fails_closed_without_live_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_ROUTINE_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_STRONG_BASE_URL", raising=False)

    response = TestClient(app).post("/chat", json={"question": "What needs attention?"})

    assert response.status_code == 503


def test_track3_request_deadline_is_strictly_below_thirty_seconds(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_REQUEST_TIMEOUT_SECONDS", "45")
    assert _request_timeout_seconds() == 29.0


def test_track3_inference_timeout_is_strictly_below_thirty_seconds(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")
    assert _timeout_seconds() == 29
