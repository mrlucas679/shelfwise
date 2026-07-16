from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls


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
                    "data_domain": _metadata_domain(metadata),
                    "title": question[:80],
                    "created_at": now,
                    "updated_at": now,
                    "messages": [],
                },
            )
            conversation.setdefault("data_domain", _metadata_domain(metadata))
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


class PostgresChatConversationStore:
    """Durable tenant/user chat store with cross-replica conversation locking."""

    def __init__(self, database_url: str, *, history_limit: int = 100) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresChatConversationStore")
        self._database_url = database_url
        self._history_limit = history_limit
        if auto_schema_enabled():
            self._ensure_schema()

    @contextmanager
    def locked(self, *, tenant_id: str, user_id: str, conversation_id: str) -> Iterator[None]:
        lock_id = _advisory_lock_id(tenant_id, user_id, conversation_id)
        with self._connect(tenant_id) as conn:
            conn.execute("select pg_advisory_lock(%s)", (lock_id,))
            try:
                yield
            finally:
                conn.execute("select pg_advisory_unlock(%s)", (lock_id,))

    def get(self, *, tenant_id: str, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select payload from shelfwise_chat_conversations
                where tenant_id = %s and user_id = %s and conversation_id = %s
                """,
                (tenant_id, user_id, conversation_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def list(self, *, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                """
                select payload from shelfwise_chat_conversations
                where tenant_id = %s and user_id = %s
                order by updated_at desc, conversation_id
                """,
                (tenant_id, user_id),
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

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
        now = datetime.now(UTC).isoformat()
        conversation = self.get(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        ) or {
            "id": conversation_id,
            "data_domain": _metadata_domain(metadata),
            "title": question[:80],
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        conversation.setdefault("data_domain", _metadata_domain(metadata))
        conversation["messages"].extend(
            [
                {"id": message_id, "role": "user", "text": question, "created_at": now},
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
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_chat_conversations
                    (tenant_id, user_id, conversation_id, payload, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, user_id, conversation_id) do update
                set payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (tenant_id, user_id, conversation_id, jsonb(conversation),
                 conversation["created_at"], now),
            )
            conn.commit()
        return deepcopy(conversation)

    def answer_for_message(
        self, *, tenant_id: str, user_id: str, conversation_id: str, message_id: str
    ) -> dict[str, Any] | None:
        conversation = self.get(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if not conversation:
            return None
        return next(
            (item for item in conversation["messages"] if item.get("reply_to") == message_id),
            None,
        )

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_chat_conversations")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_CHAT_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_chat_conversations",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_chat_store() -> ChatConversationStore | PostgresChatConversationStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return ChatConversationStore()
    if backend == "postgres":
        return PostgresChatConversationStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _advisory_lock_id(tenant_id: str, user_id: str, conversation_id: str) -> int:
    raw = f"{tenant_id}\0{user_id}\0{conversation_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _metadata_domain(metadata: dict[str, Any]) -> str:
    return str(metadata.get("data_domain") or "world_simulation")


_CHAT_SCHEMA_SQL = """
create table if not exists shelfwise_chat_conversations (
    tenant_id text not null,
    user_id text not null,
    conversation_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, user_id, conversation_id)
);
create index if not exists idx_shelfwise_chat_conversations_user_updated
on shelfwise_chat_conversations (tenant_id, user_id, updated_at desc);
"""
