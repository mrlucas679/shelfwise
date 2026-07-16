"""Typed conversation memory; live operational truth remains outside this module.

Closes the plan's flagged gap: chat previously carried only a bare sliding window of the
last few raw messages, so a long conversation silently lost everything earlier - no
rolling summary, no retrieval, no memory layer. This module adds the hierarchical layer
the Section 37/41 blueprint specifies: durable, provenance-tracked memory items per
conversation, with a deterministic rolling-summary compaction of turns that fall out of
the recent window. Compaction is deterministic-extractive (method recorded on the item),
so the durable memory never contains model invention - an LLM-composed summarizer can
later swap in behind the same store and provenance contract without changing callers.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls

SUMMARY_METHOD = "deterministic_extractive_v1"
_MAX_SUMMARY_CHARS = 2_400
_MAX_DIGEST_CHARS = 160

# Correction markers: a user turn opening with one of these supersedes earlier claims and
# must survive compaction verbatim rather than being averaged away.
_CORRECTION_PREFIXES = ("no,", "no ", "actually", "that's wrong", "thats wrong", "incorrect")


class MemoryKind(StrEnum):
    OBJECTIVE = "objective"
    USER_PREFERENCE = "user_preference"
    COMMITMENT = "commitment"
    UNRESOLVED_QUESTION = "unresolved_question"
    CORRECTION = "correction"
    EPISODE_SUMMARY = "episode_summary"
    ENTITY_REFERENCE = "entity_reference"


@dataclass(frozen=True, slots=True)
class ConversationMemoryItem:
    id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    kind: MemoryKind
    text: str
    source_message_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    valid_from: datetime
    valid_to: datetime | None
    supersedes_id: str | None
    status: str
    confidence: float
    summary_version: str
    created_by_model_run_id: str | None

    def assert_activatable(self) -> None:
        """Require provenance and prevent weak model output becoming durable memory."""
        if not self.source_message_ids:
            raise ValueError("conversation memory requires source message IDs")
        if self.status not in {"candidate", "active", "superseded", "deleted"}:
            raise ValueError("unsupported conversation memory status")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence must be between zero and one")
        if (
            self.kind is MemoryKind.COMMITMENT
            and self.status == "active"
            and self.confidence < 1.0
        ):
            raise ValueError("active commitments require explicit confirmation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind.value,
            "text": self.text,
            "source_message_ids": list(self.source_message_ids),
            "entity_ids": list(self.entity_ids),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "supersedes_id": self.supersedes_id,
            "status": self.status,
            "confidence": self.confidence,
            "summary_version": self.summary_version,
            "created_by_model_run_id": self.created_by_model_run_id,
        }


def _item_from_dict(payload: dict[str, Any]) -> ConversationMemoryItem:
    return ConversationMemoryItem(
        id=str(payload["id"]),
        tenant_id=str(payload["tenant_id"]),
        user_id=str(payload["user_id"]),
        conversation_id=str(payload["conversation_id"]),
        kind=MemoryKind(payload["kind"]),
        text=str(payload["text"]),
        source_message_ids=tuple(payload.get("source_message_ids") or ()),
        entity_ids=tuple(payload.get("entity_ids") or ()),
        valid_from=datetime.fromisoformat(str(payload["valid_from"])),
        valid_to=(
            datetime.fromisoformat(str(payload["valid_to"])) if payload.get("valid_to") else None
        ),
        supersedes_id=payload.get("supersedes_id"),
        status=str(payload["status"]),
        confidence=float(payload["confidence"]),
        summary_version=str(payload["summary_version"]),
        created_by_model_run_id=payload.get("created_by_model_run_id"),
    )


class InMemoryConversationMemoryStore:
    """Process-local memory store used by the default zero-config runtime."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[tuple[str, str, str], list[ConversationMemoryItem]] = {}

    def upsert(self, item: ConversationMemoryItem) -> ConversationMemoryItem:
        item.assert_activatable()
        key = (item.tenant_id, item.user_id, item.conversation_id)
        with self._lock:
            rows = self._items.setdefault(key, [])
            if item.supersedes_id:
                rows[:] = [
                    row
                    if row.id != item.supersedes_id
                    else _with_status(row, "superseded")
                    for row in rows
                ]
            rows[:] = [row for row in rows if row.id != item.id]
            rows.append(item)
            return item

    def active_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> ConversationMemoryItem | None:
        with self._lock:
            rows = self._items.get((tenant_id, user_id, conversation_id), [])
            summaries = [
                row
                for row in rows
                if row.kind is MemoryKind.EPISODE_SUMMARY and row.status == "active"
            ]
            return max(summaries, key=lambda row: row.summary_version) if summaries else None

    def list_active(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> list[ConversationMemoryItem]:
        with self._lock:
            rows = self._items.get((tenant_id, user_id, conversation_id), [])
            return [row for row in rows if row.status == "active"]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class PostgresConversationMemoryStore:
    """Durable memory store protected by the same tenant RLS contract as chat."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresConversationMemoryStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert(self, item: ConversationMemoryItem) -> ConversationMemoryItem:
        item.assert_activatable()
        with connect(self._database_url, tenant_id=item.tenant_id) as conn:
            if item.supersedes_id:
                conn.execute(
                    """
                    update shelfwise_chat_memory_items
                    set status = 'superseded', payload = payload || '{"status": "superseded"}'
                    where tenant_id = %s and user_id = %s and conversation_id = %s
                      and memory_id = %s
                    """,
                    (item.tenant_id, item.user_id, item.conversation_id, item.supersedes_id),
                )
            conn.execute(
                """
                insert into shelfwise_chat_memory_items
                    (tenant_id, user_id, conversation_id, memory_id, kind, status,
                     summary_version, payload, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, user_id, conversation_id, memory_id) do update
                set kind = excluded.kind,
                    status = excluded.status,
                    summary_version = excluded.summary_version,
                    payload = excluded.payload
                """,
                (
                    item.tenant_id,
                    item.user_id,
                    item.conversation_id,
                    item.id,
                    item.kind.value,
                    item.status,
                    item.summary_version,
                    jsonb(item.to_dict()),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        return item

    def active_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> ConversationMemoryItem | None:
        with connect(self._database_url, tenant_id=tenant_id) as conn:
            row = conn.execute(
                """
                select payload
                from shelfwise_chat_memory_items
                where tenant_id = %s and user_id = %s and conversation_id = %s
                  and kind = 'episode_summary' and status = 'active'
                order by summary_version desc
                limit 1
                """,
                (tenant_id, user_id, conversation_id),
            ).fetchone()
        return _item_from_dict(row["payload"]) if row else None

    def list_active(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> list[ConversationMemoryItem]:
        with connect(self._database_url, tenant_id=tenant_id) as conn:
            rows = conn.execute(
                """
                select payload
                from shelfwise_chat_memory_items
                where tenant_id = %s and user_id = %s and conversation_id = %s
                  and status = 'active'
                order by created_at, memory_id
                """,
                (tenant_id, user_id, conversation_id),
            ).fetchall()
        return [_item_from_dict(row["payload"]) for row in rows]

    def clear(self) -> None:
        with connect(self._database_url) as conn:
            conn.execute("delete from shelfwise_chat_memory_items")
            conn.commit()

    def _ensure_schema(self) -> None:
        with connect(self._database_url) as conn:
            conn.execute(
                """
                create table if not exists shelfwise_chat_memory_items (
                    tenant_id text not null,
                    user_id text not null,
                    conversation_id text not null,
                    memory_id text not null,
                    kind text not null,
                    status text not null,
                    summary_version text not null,
                    payload jsonb not null,
                    created_at timestamptz not null,
                    primary key (tenant_id, user_id, conversation_id, memory_id)
                )
                """
            )
            apply_tenant_rls(conn, ("shelfwise_chat_memory_items",))
            conn.commit()


def create_conversation_memory_store() -> (
    InMemoryConversationMemoryStore | PostgresConversationMemoryStore
):
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryConversationMemoryStore()
    if backend == "postgres":
        return PostgresConversationMemoryStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def compact_conversation(
    store: Any,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
    recent_window: int,
) -> ConversationMemoryItem | None:
    """Roll every message older than the recent window into one active episode summary.

    Deterministic and idempotent: the summary id is a hash of the covered message ids, so
    re-compacting the same prefix is a no-op upsert; a longer prefix supersedes the prior
    summary. Corrections are preserved verbatim (a user's "no, actually..." must never be
    averaged away); the first user turn is kept as the conversation objective; everything
    else becomes bounded one-line digests, newest last.
    """
    if recent_window < 1:
        raise ValueError("recent_window must be at least 1")
    older = [
        message
        for message in messages[:-recent_window]
        if isinstance(message, dict) and str(message.get("role")) in {"user", "assistant"}
    ]
    if not older:
        return None

    source_ids = tuple(str(message.get("id") or index) for index, message in enumerate(older))
    summary_id = "mem_sum_" + hashlib.sha256("|".join(source_ids).encode()).hexdigest()[:16]
    existing = store.active_summary(
        tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
    )
    if existing is not None and existing.id == summary_id:
        return existing

    lines: list[str] = []
    first_user = next((m for m in older if str(m.get("role")) == "user"), None)
    if first_user is not None:
        lines.append(f"Objective: {str(first_user.get('text') or '')[:_MAX_DIGEST_CHARS]}")
    for message in older:
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if str(message.get("role")) == "user" and lowered.startswith(_CORRECTION_PREFIXES):
            lines.append(f"Correction (verbatim): {text[:_MAX_DIGEST_CHARS * 2]}")
        else:
            lines.append(f"{message.get('role')}: {text[:_MAX_DIGEST_CHARS]}")
    body = "\n".join(lines)[:_MAX_SUMMARY_CHARS]

    item = ConversationMemoryItem(
        id=summary_id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        kind=MemoryKind.EPISODE_SUMMARY,
        text=body,
        source_message_ids=source_ids,
        entity_ids=(),
        valid_from=datetime.now(UTC),
        valid_to=None,
        supersedes_id=existing.id if existing is not None else None,
        status="active",
        confidence=1.0,
        summary_version=f"{SUMMARY_METHOD}:{len(source_ids)}",
        created_by_model_run_id=None,
    )
    return store.upsert(item)


def _with_status(item: ConversationMemoryItem, status: str) -> ConversationMemoryItem:
    return ConversationMemoryItem(
        id=item.id,
        tenant_id=item.tenant_id,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        kind=item.kind,
        text=item.text,
        source_message_ids=item.source_message_ids,
        entity_ids=item.entity_ids,
        valid_from=item.valid_from,
        valid_to=item.valid_to,
        supersedes_id=item.supersedes_id,
        status=status,
        confidence=item.confidence,
        summary_version=item.summary_version,
        created_by_model_run_id=item.created_by_model_run_id,
    )
