from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from shelfwise_backend.app import app
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
    response = TestClient(app).post(
        "/chat",
        headers=_headers(tenant_id="tenant_a", user_id="user_a"),
        json={
            "question": "What needs attention?",
            "conversation_id": "conv_a",
            "message_id": "msg_a",
        },
    )

    assert response.status_code == 200
    assert response.headers["X-ShelfWise-Conversation-ID"] == "conv_a"
    assert response.headers["X-ShelfWise-Message-ID"] == "msg_a"
    assert response.headers["X-ShelfWise-Replayed"] == "false"


def test_duplicate_message_is_idempotent_under_concurrency(monkeypatch) -> None:
    _enable_jwt(monkeypatch)
    client = TestClient(app)
    headers = _headers(tenant_id="tenant_a", user_id="user_a")
    payload = {
        "question": "What needs attention?",
        "conversation_id": "conv_duplicate",
        "message_id": "msg_duplicate",
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/chat", headers=headers, json=payload), range(8))
        )

    assert {response.status_code for response in responses} == {200}
    assert len({response.text for response in responses}) == 1
    assert sum(response.headers["X-ShelfWise-Replayed"] == "false" for response in responses) == 1
    conversation = client.get("/chat/conversations/conv_duplicate", headers=headers).json()[
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
