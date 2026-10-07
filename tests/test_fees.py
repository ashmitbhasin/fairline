import pytest

from fairvalue.fees import break_even, fee_per_contract, kelly_fraction, order_fee


def test_taker_fee_at_50c_lot():
    assert order_fee(0.50, 100, "taker") == pytest.approx(1.75)


def test_single_contract_rounds_up_to_two_cents():
    assert order_fee(0.50, 1, "taker") == pytest.approx(0.02)


def test_taker_fee_at_90c():
    assert order_fee(0.90, 100, "taker") == pytest.approx(0.63)


def test_maker_free_on_plain_quadratic_series():
    assert order_fee(0.50, 100, "maker", fee_type="quadratic") == 0.0


def test_maker_pays_on_core_sports_series():
    # KXNFLGAME/SPREAD/TOTAL are quadratic_with_maker_fees, multiplier 1 (API, 2026-10-06).
    assert order_fee(0.50, 100, "maker", fee_type="quadratic_with_maker_fees") == pytest.approx(0.44)
    assert fee_per_contract(0.50, 100, "maker", fee_type="quadratic_with_maker_fees") == pytest.approx(0.0044)


def test_break_even():
    assert break_even(0.50, 0.0175) == pytest.approx(0.5175)


def test_kelly_matches_brief_calculator_example():
    # 50c, fair 51.5%, 1c error, quarter Kelly, no fee -> 0.25% of bankroll
    assert kelly_fraction(0.515, 0.50, 0.0, fair_error=0.01, fraction=0.25) == pytest.approx(0.0025)


def test_kelly_zero_when_edge_below_cost():
    assert kelly_fraction(0.509, 0.50, 0.0175) == 0.0


def test_bad_inputs():
    with pytest.raises(ValueError):
        order_fee(1.2, 1, "taker")
    with pytest.raises(ValueError):
        order_fee(0.5, 1, "market-maker")
