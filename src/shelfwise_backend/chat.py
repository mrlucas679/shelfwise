from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_inference import InferenceError, OpenAICompatibleInferenceClient

from .product_catalog import search_product_catalog
from .security.gateway import DATA_RULE, fence_context, spotlight


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    message_id: str | None = Field(default=None, min_length=1, max_length=128)
    live_required: bool = False


def stream_chat_reply(
    *,
    question: str,
    state: dict[str, Any],
    client: OpenAICompatibleInferenceClient | None = None,
    tenant_id: str = "default",
    correlation_id: str | None = None,
    live_required: bool = False,
) -> Iterator[str]:
    """Yield a short chat answer while keeping raw user text fenced as data."""
    answer, _meta = build_chat_reply_with_meta(
        question=question,
        state=state,
        client=client,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        live_required=live_required,
    )
    yield from _chunk_words(answer)


def build_chat_reply(
    *,
    question: str,
    state: dict[str, Any],
    client: OpenAICompatibleInferenceClient | None = None,
    tenant_id: str = "default",
    correlation_id: str | None = None,
    live_required: bool = False,
) -> str:
    """Build a chat answer from current backend state."""
    answer, _meta = build_chat_reply_with_meta(
        question=question,
        state=state,
        client=client,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        live_required=live_required,
    )
    return answer


def build_chat_reply_with_meta(
    *,
    question: str,
    state: dict[str, Any],
    client: OpenAICompatibleInferenceClient | None = None,
    tenant_id: str = "default",
    correlation_id: str | None = None,
    live_required: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Answer via the real product tools first, then the model (or offline fallback).

    Every answer is grounded in the product the question actually asks about - the
    catalogue search tool runs on both the model and offline paths, and the metadata
    reports which source answered and which tools ran, so an unattended harness can
    prove what was exercised instead of guessing from answer text.
    """
    inference = client or OpenAICompatibleInferenceClient()
    subject, product, tool_calls = _tool_context(question)
    meta: dict[str, Any] = {
        "tools_used": [call["tool"] for call in tool_calls],
        "subject": subject,
        "model": getattr(inference.config, "strong_model", ""),
        "provider": getattr(getattr(inference.config, "provider", None), "value", "unknown"),
        "answer_source": "offline",
    }
    state = dict(state)
    state["tool_results"] = {"catalog_search": product, "subject": subject}
    if not inference.config.api_key_present:
        if live_required:
            raise InferenceError("live chat requires configured inference credentials")
        return (
            _offline_reply(question=question, state=state, subject=subject, product=product),
            meta,
        )
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
        if live_required:
            raise
        return (
            _offline_reply(question=question, state=state, subject=subject, product=product),
            meta,
        )
    if live_required and not result.used_network:
        raise InferenceError("live chat rejected a non-network inference result")
    answer = result.content.strip()[:2_000]
    if not answer:
        if live_required:
            raise InferenceError("live chat received an empty inference result")
        return (
            _offline_reply(question=question, state=state, subject=subject, product=product),
            meta,
        )
    meta["answer_source"] = "model"
    return answer, meta


def _extract_product_query(question: str) -> str:
    """Pull the longest Title-Case run out of the question - product names read that way."""
    tokens = question.replace("?", " ").replace(",", " ").split()
    best: list[str] = []
    current: list[str] = []
    for token in tokens:
        qualifies = (token[:1].isupper() and (token[1:].islower() or len(token) == 1)) or (
            any(ch.isdigit() for ch in token) and any(ch.isupper() for ch in token)
        )
        if qualifies:
            current.append(token)
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    return " ".join(best) if len(best) >= 2 else question[:80]


def _tool_context(question: str) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    subject = _extract_product_query(question)
    tool_calls: list[dict[str, Any]] = [{"tool": "products.search", "query": subject}]
    try:
        result = search_product_catalog(query=subject, limit=3, synthetic_scan_budget=25_000)
        products = result.get("products") or []
    except (TypeError, ValueError):
        products = []
    product = products[0] if products else None
    tool_calls[0]["hits"] = len(products)
    return subject, product, tool_calls


def _offline_reply(
    *,
    question: str,
    state: dict[str, Any],
    subject: str = "",
    product: dict[str, Any] | None = None,
) -> str:
    """Deterministic local answer for offline-safe development and tests."""
    grounding = ""
    if subject:
        grounding = f" Asked about: {subject}."
    if product:
        price = product.get("price") or {}
        grounding += (
            f" Catalogue match: {product.get('name')} ({product.get('category')}), "
            f"on hand {product.get('on_hand')}, price R{price.get('amount', '?')}."
        )
    decisions = state.get("decisions") if isinstance(state.get("decisions"), list) else []
    open_decisions = [
        item for item in decisions if isinstance(item, dict) and item.get("status") == "pending"
    ]
    latest = open_decisions[0] if open_decisions else (decisions[0] if decisions else {})
    action = latest.get("action") if isinstance(latest.get("action"), dict) else {}
    action_type = action.get("type") or "monitor"
    summary = latest.get("summary") or "No decision is currently active."
    lower = question.lower()
    if "why" in lower:
        return (
            f"The current recommendation is {action_type} because the latest evidence "
            f"says: {summary}{grounding}"
        )
    if "risk" in lower:
        return (
            f"ShelfWise is tracking {len(open_decisions)} pending high-review "
            f"decision(s). {summary}{grounding}"
        )
    return f"Current ShelfWise state: {summary}{grounding}"


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
