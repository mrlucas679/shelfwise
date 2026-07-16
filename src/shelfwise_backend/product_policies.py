"""Explicit product policies used by deterministic candidate and agent gates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProductPolicy:
    """Operational rules that vary by product family.

    Markdown parameters live HERE, not inline in cascade code: the candidate discount a
    cascade evaluates is a business rule that varies by product family, and burying it
    as a literal in decision logic makes tuning it a code hunt instead of a policy
    edit. Every family currently carries the historical 20%/24h candidate - moving
    ownership changed no decision output; changing a family's markdown now happens in
    this table only.
    """

    policy_id: str
    expiry_review_days: int
    minimum_margin_pct: int
    cold_chain_sensitive: bool
    hitl_required: bool
    markdown_discount_pct: str = "0.20"
    markdown_duration_hours: int = 24


DEFAULT_POLICY = ProductPolicy(
    policy_id="ambient_default_v1",
    expiry_review_days=3,
    minimum_margin_pct=15,
    cold_chain_sensitive=False,
    hitl_required=True,
)

_POLICIES = (
    ("bakery", ProductPolicy("bakery_same_day_v1", 1, 10, False, True)),
    ("produce", ProductPolicy("produce_quality_v1", 2, 12, False, True)),
    ("dairy", ProductPolicy("dairy_chilled_v1", 3, 15, True, True)),
    ("frozen", ProductPolicy("frozen_cold_chain_v1", 5, 18, True, True)),
    ("meat", ProductPolicy("meat_chilled_v1", 2, 18, True, True)),
    ("seafood", ProductPolicy("seafood_chilled_v1", 2, 20, True, True)),
)


def resolve_product_policy(category: str | None, physics: str | None = None) -> ProductPolicy:
    """Resolve a stable policy by category or physical storage family."""
    haystack = f"{category or ''} {physics or ''}".lower()
    for term, policy in _POLICIES:
        if term in haystack:
            return policy
    return DEFAULT_POLICY
