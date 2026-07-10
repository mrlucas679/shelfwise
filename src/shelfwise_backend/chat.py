from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_inference import InferenceError, OpenAICompatibleInferenceClient

from .security.gateway import DATA_RULE, fence_context, spotlight


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)


def stream_chat_reply(
    *,
    question: str,
    state: dict[str, Any],
    client: OpenAICompatibleInferenceClient | None = None,
    tenant_id: str = "default",
    correlation_id: str | None = None,
) -> Iterator[str]:
    """Yield a short chat answer while keeping raw user text fenced as data."""
    answer = build_chat_reply(
        question=question,
        state=state,
        client=client,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
    )
    yield from _chunk_words(answer)


def build_chat_reply(
    *,
    question: str,
    state: dict[str, Any],
    client: OpenAICompatibleInferenceClient | None = None,
    tenant_id: str = "default",
    correlation_id: str | None = None,
) -> str:
    """Build a chat answer from current backend state."""
    inference = client or OpenAICompatibleInferenceClient()
    if not inference.config.api_key_present:
        return _offline_reply(question=question, state=state)
    prompt = (
        f"{DATA_RULE}\n\n"
        f"<state_json>"
        f"{json.dumps(fence_context(state), sort_keys=True, default=str)}"
        f"</state_json>\n"
        f"<user_question>{spotlight(question, max_len=2_000)}</user_question>"
    )
    try:
        result = inference.complete(
            agent="executive",
            system="You are ShelfWise Executive chat. Be concise and evidence-grounded.",
            user=prompt,
            max_tokens=300,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
        )
    except InferenceError:
        return _offline_reply(question=question, state=state)
    return result.content.strip()[:2_000] or _offline_reply(question=question, state=state)


def _offline_reply(*, question: str, state: dict[str, Any]) -> str:
    """Deterministic local answer for offline-safe development and tests."""
    decisions = state.get("decisions") if isinstance(state.get("decisions"), list) else []
    open_decisions = [
        item
        for item in decisions
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    latest = open_decisions[0] if open_decisions else (decisions[0] if decisions else {})
    action = latest.get("action") if isinstance(latest.get("action"), dict) else {}
    action_type = action.get("type") or "monitor"
    summary = latest.get("summary") or "No decision is currently active."
    lower = question.lower()
    if "why" in lower:
        return (
            f"The current recommendation is {action_type} because the latest evidence "
            f"says: {summary}"
        )
    if "risk" in lower:
        return (
            f"ShelfWise is tracking {len(open_decisions)} pending high-review "
            f"decision(s). {summary}"
        )
    return f"Current ShelfWise state: {summary}"


def _chunk_words(text: str, *, words_per_chunk: int = 8) -> Iterator[str]:
    """Split text into small chunks so StreamingResponse behaves like a stream."""
    words = text.split()
    if not words:
        yield ""
        return
    for index in range(0, len(words), words_per_chunk):
        chunk = " ".join(words[index : index + words_per_chunk])
        suffix = " " if index + words_per_chunk < len(words) else ""
        yield chunk + suffix
