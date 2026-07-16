"""Deterministic routine/strong routing for the Shop Assistant surface.

Implements the plan's Section 41.1 blueprint exactly: no free-form model judgment ever
controls which tier answers a conversation turn - the route is computed from facts known
before inference and saved as an auditable receipt alongside the answer, so every tier
decision can be replayed and challenged after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConversationTier(StrEnum):
    ROUTINE = "routine"
    STRONG = "strong"


class RouteReason(StrEnum):
    ROUTINE_SIMPLE_FOLLOWUP = "routine_simple_followup"
    ROUTINE_SINGLE_DOMAIN_LOOKUP = "routine_single_domain_lookup"
    ROUTINE_MEMORY_COMPACTION = "routine_memory_compaction"
    STRONG_MULTI_DOMAIN = "strong_multi_domain"
    STRONG_HIGH_RISK = "strong_high_risk"
    STRONG_SCENARIO_REASONING = "strong_scenario_reasoning"
    STRONG_SOURCE_CONFLICT = "strong_source_conflict"
    STRONG_MEMORY_RECONCILIATION = "strong_memory_reconciliation"
    ESCALATED_VALIDATION_FAILED = "escalated_routine_validation_failed"
    ESCALATED_INSUFFICIENT_EVIDENCE = "escalated_insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ConversationRouteRequest:
    """Facts known before inference; no free-form model judgment controls routing."""

    domains: tuple[str, ...]
    risk_tier: str
    asks_for_scenario: bool
    has_source_conflict: bool
    has_memory_conflict: bool
    is_compaction: bool = False
    is_simple_followup: bool = False


@dataclass(frozen=True, slots=True)
class ConversationRoute:
    """Auditable model-tier decision saved before and after the model call."""

    tier: ConversationTier
    reason: RouteReason
    policy_version: str = "conversation-route-v1"

    def to_dict(self) -> dict[str, str]:
        return {
            "tier": self.tier.value,
            "reason": self.reason.value,
            "policy_version": self.policy_version,
        }


def choose_conversation_route(request: ConversationRouteRequest) -> ConversationRoute:
    """Prefer E4B for bounded work and require 31B for reasoning/risk boundaries."""
    if request.risk_tier.lower() in {"high", "critical"}:
        return ConversationRoute(ConversationTier.STRONG, RouteReason.STRONG_HIGH_RISK)
    if request.has_source_conflict:
        return ConversationRoute(ConversationTier.STRONG, RouteReason.STRONG_SOURCE_CONFLICT)
    if request.has_memory_conflict:
        return ConversationRoute(
            ConversationTier.STRONG,
            RouteReason.STRONG_MEMORY_RECONCILIATION,
        )
    if request.asks_for_scenario:
        return ConversationRoute(ConversationTier.STRONG, RouteReason.STRONG_SCENARIO_REASONING)
    if len(set(request.domains)) > 1:
        return ConversationRoute(ConversationTier.STRONG, RouteReason.STRONG_MULTI_DOMAIN)
    if request.is_compaction:
        return ConversationRoute(
            ConversationTier.ROUTINE,
            RouteReason.ROUTINE_MEMORY_COMPACTION,
        )
    if request.is_simple_followup:
        return ConversationRoute(
            ConversationTier.ROUTINE,
            RouteReason.ROUTINE_SIMPLE_FOLLOWUP,
        )
    return ConversationRoute(
        ConversationTier.ROUTINE,
        RouteReason.ROUTINE_SINGLE_DOMAIN_LOOKUP,
    )
