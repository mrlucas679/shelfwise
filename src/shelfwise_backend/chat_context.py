"""Deterministic, bounded selection of evidence for the chat prompt."""

from __future__ import annotations

import re
from typing import Any

_TERM_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "does",
        "from",
        "have",
        "more",
        "needs",
        "please",
        "show",
        "that",
        "the",
        "this",
        "what",
        "with",
    }
)
_RISK_SCORES = {"critical": 6, "high": 4, "medium": 2, "low": 0}


def bounded_chat_decisions(
    decisions: list[dict[str, Any]],
    *,
    question: str = "",
    pending_limit: int,
    resolved_limit: int,
) -> list[dict[str, Any]]:
    """Select a bounded, relevant decision set without changing stored history."""
    pending = _select_decisions(
        [item for item in decisions if item.get("status") == "pending"],
        question=question,
        limit=pending_limit,
    )
    resolved = _select_decisions(
        [item for item in decisions if item.get("status") != "pending"],
        question=question,
        limit=resolved_limit,
    )
    return [_compact_decision(item) for item in pending + resolved]


def bounded_chat_learning_events(
    events: list[dict[str, Any]],
    *,
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Select recent or question-matching learning events within a fixed budget."""
    selected = _ranked_items(events, question=question, limit=limit)
    return [_compact_learning_event(item) for item in selected]


def _select_decisions(
    decisions: list[dict[str, Any]],
    *,
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not question:
        return _recent(decisions, limit=limit)
    return _ranked_items(decisions, question=question, limit=limit)


def _ranked_items(
    items: list[dict[str, Any]],
    *,
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    terms = _terms(question)
    ranked = sorted(
        items,
        key=lambda item: _relevance_score(item, terms),
        reverse=True,
    )
    return ranked[:limit]


def _relevance_score(item: dict[str, Any], terms: set[str]) -> tuple[int, str]:
    text = _searchable_text(item)
    matches = sum(1 for term in terms if term in text)
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    risk = str(action.get("risk_tier") or item.get("risk_tier") or "").lower()
    score = matches * 5 + _RISK_SCORES.get(risk, 0)
    if item.get("status") == "pending":
        score += 2
    if str(item.get("critic_verdict") or "").lower() == "rejected":
        score += 3
    timestamp = str(item.get("updated_at") or item.get("created_at") or "")
    return score, timestamp


def _searchable_text(item: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("id", "summary", "role", "critic_verdict", "message", "metric", "decision_id"):
        value = item.get(key)
        if value is not None:
            values.append(str(value).lower())
    for key in ("action", "rejected_action", "expected_outcome", "outcome"):
        value = item.get(key)
        if isinstance(value, dict):
            values.extend(str(part).lower() for part in value)
            values.extend(str(part).lower() for part in value.values())
    return " ".join(values)


def _terms(question: str) -> set[str]:
    return {
        term.lower()
        for term in _TERM_PATTERN.findall(question)
        if term.lower() not in _STOPWORDS
    }


def _recent(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if len(items) <= limit:
        return items
    return sorted(
        items,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:limit]


def _compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "status", "summary", "role", "critic_verdict")
    compact = {key: decision[key] for key in fields if key in decision}
    action = decision.get("action")
    if isinstance(action, dict):
        compact["action"] = {
            key: action[key] for key in ("type", "risk_tier") if key in action
        }
    return compact


def _compact_learning_event(event: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "decision_id",
        "metric",
        "message",
        "created_at",
        "outcome",
        "previous_value",
        "updated_value",
    )
    return {key: event[key] for key in fields if key in event}
