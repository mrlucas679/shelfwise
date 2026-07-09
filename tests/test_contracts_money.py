from __future__ import annotations

from decimal import Decimal

import pytest

from shelfwise_contracts import Money


def test_money_zar_rounds_half_up_at_the_cent_boundary() -> None:
    assert Money.zar("1.005").minor_units == 101
    assert Money.zar("1.004").minor_units == 100
    assert Money.zar(0).minor_units == 0


def test_money_amount_converts_minor_units_back_to_a_two_decimal_rand_value() -> None:
    assert Money(minor_units=12_345, currency="ZAR").amount == Decimal("123.45")
    assert Money(minor_units=0, currency="ZAR").amount == Decimal("0.00")


def test_money_add_and_sub_operate_on_minor_units_and_allow_negative_results() -> None:
    total = Money.zar("10") + Money.zar("5")
    delta = Money.zar("1") - Money.zar("3")

    assert total.minor_units == 1_500
    assert delta.minor_units == -200


def test_money_arithmetic_rejects_mismatched_currency() -> None:
    zar = Money(minor_units=100, currency="ZAR")
    usd = Money(minor_units=100, currency="USD")

    with pytest.raises(ValueError, match="currency mismatch"):
        _ = zar + usd
    with pytest.raises(ValueError, match="currency mismatch"):
        _ = zar - usd


def test_money_mul_rounds_half_up_and_preserves_currency() -> None:
    result = Money.zar("10") * Decimal("0.205")

    assert result.minor_units == 205
    assert result.currency == "ZAR"


def test_money_str_and_to_dict_render_the_decimal_amount() -> None:
    money = Money.zar("42.5")

    assert str(money) == "ZAR 42.50"
    assert money.to_dict() == {
        "minor_units": 4_250,
        "currency": "ZAR",
        "amount": "42.50",
    }
