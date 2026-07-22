from __future__ import annotations

from decimal import Decimal

import pytest

from shelfwise_contracts import Money
from shelfwise_decision_science import (
    ActionCandidate,
    ColdChainRisk,
    ExpiryRisk,
    InventoryPolicyInput,
    Relation,
    RelationStore,
    SupplierProfile,
    compute_reorder_policy,
    detect_robust_anomaly,
    forecast_demand,
    rank_actions,
    recommend_suppliers,
    score_expiry_risk,
    serialise_tool_result,
)


def test_robust_anomaly_detection_flags_extreme_current_value() -> None:
    result = detect_robust_anomaly(
        metric_name="fridge_temp_c",
        current_value=Decimal("18"),
        history=[
            Decimal("4.8"),
            Decimal("5.0"),
            Decimal("5.1"),
            Decimal("4.9"),
            Decimal("5.2"),
            Decimal("5.0"),
        ],
    )

    assert result.is_anomaly is True
    assert result.score > Decimal("3.5")
    assert result.method == "median_absolute_deviation_robust_z_score"


def test_rank_actions_applies_risk_penalty_to_expected_value() -> None:
    ranked = rank_actions(
        [
            ActionCandidate(
                action_type="risky_supplier_switch",
                expected_value=Money.zar("1000"),
                confidence=Decimal("0.80"),
                risk_band="critical",
            ),
            ActionCandidate(
                action_type="safe_transfer",
                expected_value=Money.zar("700"),
                confidence=Decimal("0.90"),
                risk_band="low",
            ),
        ]
    )

    assert [item.candidate.action_type for item in ranked] == [
        "safe_transfer",
        "risky_supplier_switch",
    ]


def test_compute_reorder_policy_uses_available_stock_and_supplier_lead_time() -> None:
    policy = compute_reorder_policy(
        InventoryPolicyInput(
            sku="milk_2l",
            on_hand=Decimal("20"),
            committed_units=Decimal("8"),
            avg_daily_demand=Decimal("10"),
            demand_std=Decimal("2"),
            lead_time_days=Decimal("3"),
            unit_cost=Money.zar("12"),
        )
    )

    assert policy.available_to_sell_units == Decimal("12.00")
    assert policy.lead_time_demand_units == Decimal("30.00")
    assert policy.should_reorder is True
    assert policy.suggested_order_units > Decimal("20")
    assert policy.zar_exposure.amount == Decimal("216.00")


def test_reorder_at_exact_equality_suggests_at_least_one_unit() -> None:
    """available == reorder_point must not report 'reorder now, order zero units'."""
    policy = compute_reorder_policy(
        InventoryPolicyInput(
            sku="milk_2l",
            on_hand=Decimal("30"),
            avg_daily_demand=Decimal("10"),
            demand_std=Decimal("0"),
            lead_time_days=Decimal("3"),
            unit_cost=Money.zar("12"),
        )
    )

    assert policy.should_reorder is True
    assert policy.units_below_reorder == Decimal("0.00")
    assert policy.suggested_order_units >= Decimal("1")


def test_expiry_risk_postcondition_rejects_an_out_of_range_risk_score() -> None:
    """The dataclass must fail loudly, not silently carry a mathematically-impossible
    risk score into a real markdown decision - proves the __post_init__ guard actually
    runs, not just that it exists in source.
    """
    with pytest.raises(ValueError, match=r"risk out of \[0,1\]"):
        ExpiryRisk(
            sku="SKU-1",
            risk=Decimal("1.5"),
            waste_units=Decimal("0"),
            zar_at_risk=Money.zar(Decimal("0")),
            effective_days_to_expiry=Decimal("1"),
            method="test",
            confidence=Decimal("0.5"),
        )


def test_expiry_risk_postcondition_rejects_negative_waste_units() -> None:
    with pytest.raises(ValueError, match="waste_units must be >= 0"):
        ExpiryRisk(
            sku="SKU-1",
            risk=Decimal("0.5"),
            waste_units=Decimal("-1"),
            zar_at_risk=Money.zar(Decimal("0")),
            effective_days_to_expiry=Decimal("1"),
            method="test",
            confidence=Decimal("0.5"),
        )


def test_cold_chain_risk_postcondition_rejects_an_out_of_range_risk_score() -> None:
    with pytest.raises(ValueError, match=r"risk out of \[0,1\]"):
        ColdChainRisk(
            area="fridge_a",
            risk=Decimal("-0.1"),
            penalty_days=Decimal("0"),
            method="test",
            confidence=Decimal("0.5"),
        )


def test_cold_chain_risk_postcondition_rejects_negative_penalty_days() -> None:
    with pytest.raises(ValueError, match="penalty_days must be >= 0"):
        ColdChainRisk(
            area="fridge_a",
            risk=Decimal("0.5"),
            penalty_days=Decimal("-1"),
            method="test",
            confidence=Decimal("0.5"),
        )


def test_score_expiry_risk_weights_sum_to_one_not_over() -> None:
    """Cold-chain risk's own contribution must be its exact weight (not > 1 combined)."""
    common = {
        "sku": "4011",
        "units_on_hand": Decimal("10"),
        "days_to_expiry": Decimal("30"),
        "forecast_daily_units": Decimal("5"),
        "unit_cost": Money.zar("10"),
        "cold_chain_penalty_days": Decimal("0"),
    }

    no_cold_chain = score_expiry_risk(cold_chain_risk=Decimal("0"), **common)
    full_cold_chain = score_expiry_risk(cold_chain_risk=Decimal("1"), **common)

    assert no_cold_chain.risk == Decimal("0.00")
    assert full_cold_chain.risk == Decimal("0.25")


def test_forecast_demand_payday_multiplier_is_opt_in_not_default_behavior() -> None:
    """The payday uplift belongs to the golden payday scenario; other callers must pass 1."""
    no_multiplier = forecast_demand(
        sku="4011", recent_daily_units=[Decimal("10"), Decimal("10")], horizon_days=3
    )
    payday_uplift = forecast_demand(
        sku="4011",
        recent_daily_units=[Decimal("10"), Decimal("10")],
        horizon_days=3,
        payday_multiplier=Decimal("1.35"),
    )
    assert no_multiplier.daily_units == Decimal("10.00")
    assert payday_uplift.daily_units == Decimal("13.50")


def test_supplier_recommendation_uses_graph_candidates_and_measured_profiles() -> None:
    graph = RelationStore()
    graph.add(Relation("sku:4011", "supplied_by", "supplier:a"))
    graph.add(Relation("sku:4011", "supplied_by", "supplier:b"))
    profiles = {
        "supplier:a": SupplierProfile(
            supplier_id="supplier:a",
            lead_time_days=Decimal("3"),
            fill_rate=Decimal("0.90"),
            unit_cost=Money.zar("10"),
        ),
        "supplier:b": SupplierProfile(
            supplier_id="supplier:b",
            lead_time_days=Decimal("1"),
            fill_rate=Decimal("0.70"),
            unit_cost=Money.zar("9"),
        ),
    }

    ranking = recommend_suppliers("4011", graph, profiles)

    assert ranking.coverage == Decimal("1.00")
    assert ranking.ranked[0].supplier_id == "supplier:b"
    assert graph.paths("sku:4011", "supplier:a") == [["sku:4011", "supplier:a"]]


def test_tool_serialiser_preserves_money_shape() -> None:
    policy = compute_reorder_policy(
        InventoryPolicyInput(
            sku="milk_2l",
            on_hand=Decimal("20"),
            avg_daily_demand=Decimal("10"),
            demand_std=Decimal("2"),
            lead_time_days=Decimal("3"),
            unit_cost=Money.zar("12"),
        )
    )

    payload = serialise_tool_result(policy)

    assert payload["zar_exposure"]["currency"] == "ZAR"
    assert payload["zar_exposure"]["amount"] == "120.00"
