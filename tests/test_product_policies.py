from __future__ import annotations

from shelfwise_backend.product_policies import DEFAULT_POLICY, resolve_product_policy


def test_product_policy_resolves_family_specific_expiry_rules() -> None:
    dairy = resolve_product_policy("Dairy", "chilled")
    bakery = resolve_product_policy("Bakery", None)

    assert dairy.policy_id == "dairy_chilled_v1"
    assert dairy.cold_chain_sensitive is True
    assert bakery.expiry_review_days == 1


def test_unknown_product_policy_is_explicitly_defaulted() -> None:
    assert resolve_product_policy("hardware", None) == DEFAULT_POLICY
