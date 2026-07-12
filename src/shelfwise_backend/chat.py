from __future__ import annotations

import asyncio
import json
import unicodedata
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_inference import InferenceError, OpenAICompatibleInferenceClient
from shelfwise_inference.orchestration import (
    AgentOrchestrationError,
    AgentOrchestrator,
    ExecutionMode,
)
from shelfwise_inference.tool_calling import (
    ToolCallingError,
    assert_conclusion_grounded_in_tool_results,
)
from shelfwise_worldgen import create_world_snapshot_store

from .product_catalog import get_delivery_exception, search_product_catalog
from .security.gateway import DATA_RULE, fence_context, spotlight
from .tools.mcp_surface import AuditLog, build_platform_tools
from .tools.model_runtime import OpenAIModelRuntime, architecture_from_inference_config
from .world_facts import WorldFactsProvider

_default_facts_store: Any = None


def _default_facts() -> WorldFactsProvider:
    global _default_facts_store
    if _default_facts_store is None:
        _default_facts_store = create_world_snapshot_store()
    return WorldFactsProvider(_default_facts_store)


_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 3_000},
    },
    "required": ["answer"],
    "additionalProperties": False,
}

_CHAT_SYSTEM_PROMPT = (
    "You are ShelfWise, an AI operations assistant for a real supermarket. Speak like a "
    "knowledgeable colleague, not a database readout.\n\n"
    "All user-facing text must be written in English. Do not translate into another "
    "language, even if the user asks; if needed, explain that this service responds in "
    "English.\n\n"
    "Format for readability: use short headings for multi-part answers, bullet or "
    "numbered lists when there is more than one item, and **bold** for the one or two "
    "figures that matter most. Keep single-fact answers to a short paragraph - only add "
    "structure when there is genuinely more than one point to make. Never describe the "
    'shape of tool_results/state_json to the user (no "the tool result is `null`", no '
    "field names, no backticks around raw internal values, no mention of JSON) - speak in "
    "plain retail-operations language.\n\n"
    "You have real, live tools covering every part of the store - call the one that "
    "matches the question before answering, never guess a number a tool could have given "
    "you: get_stock (on-hand/on-order for a SKU), get_demand_forecast, get_expiry_risk, "
    "get_reorder_policy and get_supplier_ranking (procurement/ordering), "
    "get_stock_sourcing_options (ranks real branches/DC/suppliers for a shortage), "
    "get_cold_chain_status (refrigeration risk), check_price_integrity (till price vs "
    "catalogue), simulate_markdown (what-if discount math), list_open_decisions and "
    "explain_decision (approvals/HITL), and get_thresholds (learned policy memory). If "
    "neither state_json nor your tools cover the question's specific subject, say plainly "
    "that you don't have data on that exact subject, then offer what you do know (open "
    "decisions, learned thresholds, recent state) instead of describing an empty result.\n\n"
    "Never simply recommend moving stock. If a shortage needs covering, call "
    "get_stock_sourcing_options first to find where the replacement stock should "
    "actually come from - it checks nearby branches, the regional distribution centre, "
    "and approved suppliers, and ranks them by availability, distance, and lead time. "
    "State which source you're recommending and why it beat the alternatives (e.g. "
    "closer, faster, cheaper, or simply the only one with stock) - never present a "
    "transfer quantity with no source attached. If the tool reports no source can cover "
    "it, say so plainly and recommend a purchase order instead of a transfer."
)


def ensure_english_response(answer: str) -> str:
    """Reject model output whose script is clearly not English-compatible."""
    text = answer.strip()
    if not text:
        raise InferenceError("model returned an empty response")
    letters = [char for char in text if char.isalpha()]
    if letters:
        latin_letters = sum(
            "LATIN" in unicodedata.name(char, "") or char.isascii() for char in letters
        )
        if latin_letters / len(letters) < 0.8:
            raise InferenceError("model returned a non-English response")
    return text


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
    decisions: Any = None,
    memory: Any = None,
    orchestrator_factory: Any = None,
) -> Iterator[str]:
    """Yield a short chat answer while keeping raw user text fenced as data."""
    answer, _meta = build_chat_reply_with_meta(
        question=question,
        state=state,
        client=client,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        live_required=live_required,
        decisions=decisions,
        memory=memory,
        orchestrator_factory=orchestrator_factory,
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
    decisions: Any = None,
    memory: Any = None,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> str:
    """Build a chat answer from current backend state."""
    answer, _meta = build_chat_reply_with_meta(
        question=question,
        state=state,
        client=client,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        live_required=live_required,
        decisions=decisions,
        memory=memory,
        facts=facts,
        orchestrator_factory=orchestrator_factory,
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
    decisions: Any = None,
    memory: Any = None,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Answer using a real agentic tool-calling loop when a decision/memory store is
    available, falling back to a single grounded completion (or the offline reply)
    otherwise.

    Passing `decisions`/`memory` (the live decision and learning stores) gives the model
    the same read-only platform tools the production cascades use - stock, demand,
    expiry, cold-chain, procurement/supplier, pricing, HITL, and learned thresholds - so
    chat can genuinely answer questions about any part of the store, not just the product
    the question happens to name. Every answer is grounded: any computed number a tool
    call returns must actually be cited in the reply, or the run is rejected the same way
    the agentic cascades are.
    """
    inference = client or OpenAICompatibleInferenceClient()
    resolved_facts = facts or _default_facts()
    subject, product, tool_calls = _tool_context(
        question, facts=resolved_facts, tenant_id=tenant_id
    )
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
            _offline_reply(
                question=question,
                state=state,
                subject=subject,
                product=product,
                facts=resolved_facts,
                tenant_id=tenant_id,
            ),
            meta,
        )
    prompt = (
        f"{DATA_RULE}\n\n"
        f"<state_json>"
        f"{json.dumps(fence_context(state), sort_keys=True, default=str)}"
        f"</state_json>\n"
        f"<user_question>{spotlight(question, max_len=2_000)}</user_question>"
    )
    if decisions is not None and memory is not None:
        last_error: AgentOrchestrationError | ToolCallingError | None = None
        for attempt in range(2):
            try:
                answer, run_tool_calls = asyncio.run(
                    _run_agentic_chat(
                        prompt=prompt,
                        inference=inference,
                        decisions=decisions,
                        memory=memory,
                        facts=resolved_facts,
                        tenant_id=tenant_id,
                        correlation_id=(
                            correlation_id if attempt == 0 else f"{correlation_id}:retry-{attempt}"
                        ),
                        orchestrator_factory=orchestrator_factory,
                    )
                )
                meta["answer_source"] = "model"
                meta["tools_used"] = [call.name for call in run_tool_calls]
                return answer, meta
            except (AgentOrchestrationError, ToolCallingError) as exc:
                last_error = exc
        assert last_error is not None
        if last_error is not None:
            if live_required:
                raise InferenceError(f"live agentic chat failed: {last_error}") from last_error
            return (
                _offline_reply(
                    question=question,
                    state=state,
                    subject=subject,
                    product=product,
                    facts=resolved_facts,
                    tenant_id=tenant_id,
                ),
                meta,
            )
    try:
        result = inference.complete(
            agent="executive",
            system=(
                "You are ShelfWise Executive chat. Be concise and evidence-grounded. "
                "Never describe the shape of tool_results/state_json to the user (no "
                '"the tool result is `null`", no field names, no backticks around raw '
                "values) - speak in plain retail-operations language, the way a store "
                "manager would talk to a colleague. If the question's subject has no "
                "catalogue match or no dedicated data (tool_results.catalog_search is "
                "null and store_intelligence/decisions do not cover it), say plainly "
                "that you don't have data on that specific subject, then pivot to what "
                "you do know from decisions/store_intelligence/learning in state_json - "
                "never leave the user with only a description of an empty result."
            ),
            user=prompt,
            max_tokens=300,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
        )
    except InferenceError:
        if live_required:
            raise
        return (
            _offline_reply(
                question=question,
                state=state,
                subject=subject,
                product=product,
                facts=resolved_facts,
                tenant_id=tenant_id,
            ),
            meta,
        )
    if live_required and not result.used_network:
        raise InferenceError("live chat rejected a non-network inference result")
    answer = ensure_english_response(result.content)[:2_000]
    if not answer:
        if live_required:
            raise InferenceError("live chat received an empty inference result")
        return (
            _offline_reply(
                question=question,
                state=state,
                subject=subject,
                product=product,
                facts=resolved_facts,
                tenant_id=tenant_id,
            ),
            meta,
        )
    meta["answer_source"] = "model"
    return answer, meta


async def _run_agentic_chat(
    *,
    prompt: str,
    inference: OpenAICompatibleInferenceClient,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider,
    tenant_id: str,
    correlation_id: str | None,
    orchestrator_factory: Any,
) -> tuple[str, tuple[Any, ...]]:
    """Run chat through the real platform-tool registry and return a grounded answer."""
    tools = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=AuditLog(),
        facts=facts,
        tenant_id=tenant_id,
    )
    orchestrator: AgentOrchestrator = (
        orchestrator_factory()
        if orchestrator_factory is not None
        else _default_chat_orchestrator(tools=tools, inference=inference)
    )
    run = await orchestrator.run(
        role="chat",
        system=_CHAT_SYSTEM_PROMPT,
        user=prompt,
        final_schema=_CHAT_SCHEMA,
        final_schema_name="chat_answer",
        correlation_id=correlation_id or f"chat_{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        temperature=0.2,
        max_tokens=900,
    )
    answer = ensure_english_response(str(run.answer["answer"]))
    assert_conclusion_grounded_in_tool_results(answer, run.tool_calls)
    if not answer:
        raise AgentOrchestrationError("agentic chat produced an empty answer")
    return answer[:3_000], run.tool_calls


def _default_chat_orchestrator(
    *, tools: list[Any], inference: OpenAICompatibleInferenceClient
) -> AgentOrchestrator:
    architecture = architecture_from_inference_config(inference.config)
    runtime = OpenAIModelRuntime(
        architecture=architecture,
        execution_mode=ExecutionMode.LIVE_REQUIRED,
        client=inference,
    )
    return AgentOrchestrator(tools=tools, model_runtime=runtime)


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


def _tool_context(
    question: str, *, facts: WorldFactsProvider, tenant_id: str
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    subject = _extract_product_query(question)
    tool_calls: list[dict[str, Any]] = [{"tool": "products.search", "query": subject}]
    try:
        result = search_product_catalog(facts=facts, query=subject, limit=3, tenant_id=tenant_id)
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
    facts: WorldFactsProvider | None = None,
    tenant_id: str = "",
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
    lower_question = question.lower()
    if "deliver" in lower_question and product and facts is not None and tenant_id:
        exception = get_delivery_exception(
            facts=facts, tenant_id=tenant_id, sku=product.get("sku", "")
        )
        if exception is not None:
            return (
                f"{exception['product_name']}'s delivery is {exception['status']}: "
                f"{exception['ordered_units']} ordered, {exception['received_units']} received, "
                f"{exception['accepted_units']} accepted, {exception['missing_units']} short. "
                f"{exception['conclusion']}{grounding}"
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
