from __future__ import annotations

from decimal import Decimal

from shelfwise_data import load_seeded_scenario, validate_seed_data


def test_seed_data_validates_the_planted_story() -> None:
    validate_seed_data()

    scenario = load_seeded_scenario()

    assert scenario.sku == "4011"
    assert scenario.product_name == "Amasi 2L"
    assert scenario.location == "store_12"
    assert scenario.units_on_hand == 240
    assert scenario.days_to_expiry == 3
    assert scenario.recent_daily_units == (
        Decimal("28"),
        Decimal("31"),
        Decimal("29"),
        Decimal("34"),
        Decimal("30"),
    )
    assert scenario.unit_price.amount == Decimal("30.00")
    assert scenario.unit_cost.amount == Decimal("18.00")
