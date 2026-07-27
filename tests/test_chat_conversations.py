from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from shelfwise_backend.app import _chunk_answer_for_delta_events, app
from shelfwise_backend.chat_store import ChatConversationStore
from shelfwise_backend.tenant import encode_hs256_token


def _headers(*, tenant_id: str, user_id: str) -> dict[str, str]:
    token = encode_hs256_token(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": "manager",
            "exp": int(time.time()) + 3600,
        },
        secret="secret",
    )
    return {"Authorization": f"Bearer {token}"}


def _enable_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def test_chat_returns_stable_conversation_and_message_identity(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    # Per-run ids: against a persistent shared Postgres, a fixed conversation/message id
    # from a prior run is CORRECTLY replayed (Replayed: true is the durable-idempotency
    # feature working across restarts) - rerun-safety requires fresh identity.
    from uuid import uuid4

    conversation_id = f"conv_a_{uuid4().hex[:10]}"
    message_id = f"msg_a_{uuid4().hex[:10]}"
    response = TestClient(app).post(
        "/chat",
        headers=_headers(tenant_id="tenant_a", user_id="user_a"),
        json={
            "question": "What needs attention?",
            "conversation_id": conversation_id,
            "message_id": message_id,
        },
    )

    assert response.status_code == 200
    assert response.headers["X-ShelfWise-Conversation-ID"] == conversation_id
    assert response.headers["X-ShelfWise-Message-ID"] == message_id
    assert response.headers["X-ShelfWise-Replayed"] == "false"


def test_duplicate_message_is_idempotent_under_concurrency(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    client = TestClient(app)
    headers = _headers(tenant_id="tenant_a", user_id="user_a")
    from uuid import uuid4

    conversation_id = f"conv_duplicate_{uuid4().hex[:10]}"
    payload = {
        "question": "What needs attention?",
        "conversation_id": conversation_id,
        "message_id": f"msg_duplicate_{uuid4().hex[:10]}",
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/chat", headers=headers, json=payload), range(8))
        )

    assert {response.status_code for response in responses} == {200}
    assert len({response.text for response in responses}) == 1
    assert sum(response.headers["X-ShelfWise-Replayed"] == "false" for response in responses) == 1
    conversation = client.get(f"/chat/conversations/{conversation_id}", headers=headers).json()[
        "conversation"
    ]
    assert len(conversation["messages"]) == 2


def test_conversations_are_isolated_by_tenant_and_user(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    client = TestClient(app)
    owner = _headers(tenant_id="tenant_a", user_id="user_a")
    other_user = _headers(tenant_id="tenant_a", user_id="user_b")
    other_tenant = _headers(tenant_id="tenant_b", user_id="user_a")
    payload = {
        "question": "Show my current risk",
        "conversation_id": "shared_name",
        "message_id": "msg_owner",
    }

    assert client.post("/chat", headers=owner, json=payload).status_code == 200
    assert client.get("/chat/conversations/shared_name", headers=owner).status_code == 200
    assert client.get("/chat/conversations/shared_name", headers=other_user).status_code == 404
    assert client.get("/chat/conversations/shared_name", headers=other_tenant).status_code == 404
    assert client.get("/chat/conversations", headers=other_user).json()["conversations"] == []


def test_conversation_cannot_mix_live_and_simulation_context(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    client = TestClient(app)
    headers = _headers(tenant_id="tenant_a", user_id="user_a")

    first = client.post(
        "/chat",
        headers=headers,
        json={
            "question": "What needs attention?",
            "conversation_id": "domain_locked",
            "message_id": "msg_world",
            "data_domain": "world_simulation",
        },
    )
    mixed = client.post(
        "/chat",
        headers=headers,
        json={
            "question": "Now check the live store",
            "conversation_id": "domain_locked",
            "message_id": "msg_live",
            "data_domain": "operational_twin",
        },
    )

    assert first.status_code == 200
    assert first.headers["X-ShelfWise-Data-Domain"] == "world_simulation"
    assert mixed.status_code == 409
    assert mixed.json()["detail"] == "Start a new conversation when changing the data source"


def test_conversation_lists_are_filtered_by_data_domain(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    client = TestClient(app)
    headers = _headers(tenant_id="tenant_domain_list", user_id="user_a")

    for domain in ("world_simulation", "operational_twin"):
        response = client.post(
            "/chat",
            headers=headers,
            json={
                "question": f"Check {domain}",
                "conversation_id": f"conv_{domain}",
                "message_id": f"msg_{domain}",
                "data_domain": domain,
            },
        )
        assert response.status_code == 200

    simulation = client.get(
        "/chat/conversations?data_domain=world_simulation", headers=headers
    ).json()
    operational = client.get(
        "/chat/conversations?data_domain=operational_twin", headers=headers
    ).json()

    assert [item["id"] for item in simulation["conversations"]] == [
        "conv_world_simulation"
    ]
    assert simulation["conversations"][0]["data_domain"] == "world_simulation"
    assert [item["id"] for item in operational["conversations"]] == [
        "conv_operational_twin"
    ]
    assert operational["conversations"][0]["data_domain"] == "operational_twin"


def test_chat_store_bounds_persisted_history() -> None:
    store = ChatConversationStore(history_limit=6)
    for index in range(10):
        store.append_exchange(
            tenant_id="tenant",
            user_id="user",
            conversation_id="conversation",
            message_id=f"msg_{index}",
            question=f"Question {index}",
            answer=f"Answer {index}",
            metadata={},
        )

    conversation = store.get(tenant_id="tenant", user_id="user", conversation_id="conversation")
    assert conversation is not None
    assert len(conversation["messages"]) == 6
    assert conversation["messages"][0]["id"] == "msg_7"


def test_chunk_answer_for_delta_events_reconstitutes_exactly() -> None:
    assert "".join(_chunk_answer_for_delta_events("")) == ""
    assert "".join(_chunk_answer_for_delta_events("one")) == "one"
    long_answer = " ".join(f"word{i}" for i in range(37))
    assert "".join(_chunk_answer_for_delta_events(long_answer)) == long_answer
    assert len(_chunk_answer_for_delta_events(long_answer, words_per_chunk=4)) > 1


def test_chat_stream_emits_a_truthful_lifecycle_envelope(monkeypatch) -> None:
    """SSE streaming, honestly: accepted -> delta* (the validated reply, delivered
    incrementally, only once it truly exists) -> answer (the same complete text once
    more, for backward compatibility) -> done (the same receipts POST /chat returns).

    The `delta` events chunk the already-validated answer rather than dribbling live,
    unvalidated provider tokens - see `_chunk_answer_for_delta_events`'s docstring for why
    that is the correct (not merely convenient) design given the shared agentic
    tool-calling loop every cascade also depends on. Concatenating every `delta` chunk
    must reconstitute the exact validated answer, byte for byte.
    """
    import json as _json
    from uuid import uuid4

    _enable_jwt(monkeypatch)
    client = TestClient(app)
    conversation_id = f"conv_stream_{uuid4().hex[:10]}"

    response = client.post(
        "/chat/stream",
        headers=_headers(tenant_id="tenant_a", user_id="user_a"),
        json={
            "question": "What needs attention?",
            "conversation_id": conversation_id,
            "message_id": f"msg_stream_{uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "event: accepted" in text
    assert "event: delta" in text
    assert "event: answer" in text
    assert "event: done" in text
    assert '"correlation_id"' in text

    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_line, data_line = block.split("\n", 1)
        event_name = event_line.removeprefix("event: ")
        payload = _json.loads(data_line.removeprefix("data: "))
        events.append((event_name, payload))

    delta_text = "".join(payload["text"] for name, payload in events if name == "delta")
    answer_text = next(payload["text"] for name, payload in events if name == "answer")
    assert delta_text == answer_text, "concatenated deltas must reconstitute the full answer"
    assert events[0][0] == "accepted"
    assert events[-1][0] == "done"
    assert events[-2][0] == "answer", "the terminal answer event must arrive right before done"

    # Idempotent duplicate must be announced as a replay, not a fresh answer.
    replay = client.post(
        "/chat/stream",
        headers=_headers(tenant_id="tenant_a", user_id="user_a"),
        json={
            "question": "What needs attention?",
            "conversation_id": conversation_id,
            "message_id": text.split('"message_id": "')[1].split('"')[0],
        },
    )
    assert "event: replayed" in replay.text
    assert "event: delta" in replay.text


def test_stream_chat_deltas_parses_real_wire_chunks_and_fails_closed_offline(
    monkeypatch,
) -> None:
    """The token-delta parser consumes genuine OpenAI-compatible SSE chunks and refuses
    to run against an offline provider - streaming never fabricates generation."""
    import io

    import pytest as _pytest

    from shelfwise_inference.client import (
        InferenceError,
        OpenAICompatibleInferenceClient,
        stream_chat_deltas,
    )

    offline = OpenAICompatibleInferenceClient()
    with _pytest.raises(InferenceError, match="live endpoint"):
        list(stream_chat_deltas(offline, agent="chat", system="s", user="u"))

    monkeypatch.setenv("LLM_BASE_URL", "https://vllm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gemma-test")
    wire = io.BytesIO(
        b'data: {"choices":[{"delta":{"content":"Stock"}}]}\n\n'
        b": keep-alive comment\n\n"
        b'data: {"choices":[{"delta":{"content":" is 12 units"}}]}\n\n'
        b"data: [DONE]\n\n"
        b'data: {"choices":[{"delta":{"content":"NEVER-EMITTED"}}]}\n\n'
    )

    class _Resp:
        def __enter__(self):
            return wire

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "shelfwise_inference.client._open_inference_request", lambda request, timeout=None: _Resp()
    )
    live = OpenAICompatibleInferenceClient()
    deltas = list(stream_chat_deltas(live, agent="chat", system="s", user="u"))
    assert deltas == ["Stock", " is 12 units"], (
        "every yielded delta must be a real wire chunk, ended by [DONE]"
    )
