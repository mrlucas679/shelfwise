"""Bounded, evidence-aware context assembly for agent and chat prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_CONTEXT_CHARS = 24_000
MAX_LIST_ITEMS = 32
MAX_STRING_CHARS = 1_200
_PRIORITY_KEYS = (
    "tenant_id",
    "subject",
    "tool_results",
    "decisions",
    "learning_events",
    "source_refs",
    # Conversational continuity must survive priority pruning: dropping the rolling
    # summary or recent turns under budget pressure silently severs the conversation -
    # the exact failure the hierarchical-memory layer exists to prevent.
    "conversation_summary",
    "conversation_history",
    "skill_catalogue",
)


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Compact prompt context plus an auditable assembly manifest."""

    payload: dict[str, Any]
    source_refs: tuple[str, ...]
    missing_data: tuple[str, ...]
    token_estimate: int
    evidence_score: float
    manifest: dict[str, Any]


def assemble_context(
    state: dict[str, Any],
    *,
    decision_type: str,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> ContextBundle:
    """Build bounded context without dropping the highest-value evidence first."""
    if not decision_type.strip():
        raise ValueError("decision_type is required")
    if max_chars <= 0 or max_chars > MAX_CONTEXT_CHARS:
        raise ValueError(f"max_chars must be between 1 and {MAX_CONTEXT_CHARS}")

    original = _compact_value(state)
    compact = original
    if not isinstance(compact, dict):
        compact = {}
    encoded = _encode(compact)
    if len(encoded) > max_chars:
        compact = _priority_subset(compact)
        encoded = _encode(compact)
    if len(encoded) > max_chars:
        compact = _fit_context(compact, max_chars)
        encoded = _encode(compact)
    source_refs = _source_refs(state)
    missing_data = _missing_data(state)
    evidence_score = _evidence_score(state, missing_data)
    token_estimate = max(1, (len(encoded) + 3) // 4)
    manifest = {
        "decision_type": decision_type,
        "source_refs": list(source_refs),
        "missing_data": list(missing_data),
        "token_estimate": token_estimate,
        "evidence_score": evidence_score,
        "context_chars": len(encoded),
        "truncated": encoded != _encode(original),
    }
    compact["context_manifest"] = manifest
    return ContextBundle(
        payload=compact,
        source_refs=source_refs,
        missing_data=missing_data,
        token_estimate=token_estimate,
        evidence_score=evidence_score,
        manifest=manifest,
    )


def _compact_value(
    value: Any,
    *,
    depth: int = 0,
    list_items: int = MAX_LIST_ITEMS,
    string_chars: int = MAX_STRING_CHARS,
) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        return value[:string_chars]
    if isinstance(value, dict):
        return {
            str(key): _compact_value(
                item,
                depth=depth + 1,
                list_items=list_items,
                string_chars=string_chars,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _compact_value(
                item,
                depth=depth + 1,
                list_items=list_items,
                string_chars=string_chars,
            )
            for item in value[:list_items]
        ]
    if isinstance(value, tuple):
        return [
            _compact_value(
                item,
                depth=depth + 1,
                list_items=list_items,
                string_chars=string_chars,
            )
            for item in value[:list_items]
        ]
    return value


def _fit_context(value: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Shrink the priority payload until its encoded form meets the hard cap."""
    priority = _priority_subset(value)
    for list_items, string_chars in ((16, 600), (8, 240), (4, 80), (1, 20)):
        candidate = _compact_value(
            priority,
            list_items=list_items,
            string_chars=string_chars,
        )
        if len(_encode(candidate)) <= max_chars:
            return candidate
    if max_chars >= len(_encode({"context": "[truncated]"})):
        return {"context": "[truncated]"}
    return {}


def _priority_subset(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state[key] for key in _PRIORITY_KEYS if key in state}


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _source_refs(state: dict[str, Any]) -> tuple[str, ...]:
    refs = state.get("source_refs")
    if not isinstance(refs, list):
        return ()
    return tuple(str(ref)[:200] for ref in refs[:32] if str(ref).strip())


def _missing_data(state: dict[str, Any]) -> tuple[str, ...]:
    missing = []
    for key in ("tool_results", "decisions"):
        if not state.get(key):
            missing.append(key)
    return tuple(missing)


def _evidence_score(state: dict[str, Any], missing_data: tuple[str, ...]) -> float:
    available = sum(bool(state.get(key)) for key in ("tool_results", "decisions", "source_refs"))
    score = available / 3
    if missing_data:
        score -= 0.1 * len(missing_data)
    return round(max(0.0, min(1.0, score)), 3)
