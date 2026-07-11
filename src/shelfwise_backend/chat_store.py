from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any


class ChatConversationStore:
    """Thread-safe, user-scoped chat history with per-conversation serialization."""

    def __init__(self, *, history_limit: int = 100) -> None:
        self._history_limit = history_limit
        self._guard = RLock()
        self._conversations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._locks: dict[tuple[str, str, str], RLock] = {}

    @contextmanager
    def locked(self, *, tenant_id: str, user_id: str, conversation_id: str) -> Iterator[None]:
        key = (tenant_id, user_id, conversation_id)
        with self._guard:
            lock = self._locks.setdefault(key, RLock())
        with lock:
            yield

    def get(self, *, tenant_id: str, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        key = (tenant_id, user_id, conversation_id)
        with self._guard:
            value = self._conversations.get(key)
            return deepcopy(value) if value else None

    def list(self, *, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        with self._guard:
            values = [
                deepcopy(value)
                for (tenant, user, _), value in self._conversations.items()
                if tenant == tenant_id and user == user_id
            ]
        return sorted(values, key=lambda item: item["updated_at"], reverse=True)

    def append_exchange(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        message_id: str,
        question: str,
        answer: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        key = (tenant_id, user_id, conversation_id)
        now = datetime.now(UTC).isoformat()
        with self._guard:
            conversation = self._conversations.setdefault(
                key,
                {
                    "id": conversation_id,
                    "title": question[:80],
                    "created_at": now,
                    "updated_at": now,
                    "messages": [],
                },
            )
            conversation["messages"].extend(
                [
                    {
                        "id": message_id,
                        "role": "user",
                        "text": question,
                        "created_at": now,
                    },
                    {
                        "id": f"{message_id}:answer",
                        "reply_to": message_id,
                        "role": "assistant",
                        "text": answer,
                        "created_at": now,
                        "metadata": deepcopy(metadata),
                    },
                ]
            )
            conversation["messages"] = conversation["messages"][-self._history_limit :]
            conversation["updated_at"] = now
            return deepcopy(conversation)

    def answer_for_message(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        conversation = self.get(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not conversation:
            return None
        return next(
            (
                message
                for message in conversation["messages"]
                if message.get("reply_to") == message_id
            ),
            None,
        )

    def clear(self) -> None:
        with self._guard:
            self._conversations.clear()
            self._locks.clear()
