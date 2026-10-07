"""The devig library must reproduce the brief's Fig. 3 table to the hundredth of a percent."""
import pytest

from fairvalue.devig import additive, all_methods, devig_american, method_spread, multiplicative, power, shin
from fairvalue.odds import american_to_decimal, american_to_prob, prob_to_american

# (favorite, underdog) -> favorite's fair probability (%) by method: multiplicative, additive, power
FIG3 = [
    ((-110, -110), (50.00, 50.00, 50.00)),
    ((-150, 130), (57.98, 58.26, 58.40)),
    ((-300, 240), (71.83, 72.79, 73.31)),
    ((-450, 340), (78.26, 79.55, 80.27)),
    ((-1000, 600), (86.42, 88.31, 89.53)),
]


@pytest.mark.parametrize("prices,expected", FIG3)
def test_fig3_table(prices, expected):
    got = all_methods(prices)
    for method, want in zip(("multiplicative", "additive", "power"), expected):
        assert round(got[method][0] * 100, 2) == want, method


@pytest.mark.parametrize("prices,_", FIG3)
def test_shin_equals_additive_on_two_way(prices, _):
    got = all_methods(prices)
    assert got["shin"][0] == pytest.approx(got["additive"][0], abs=5e-5)


@pytest.mark.parametrize("fn", [multiplicative, additive, power, shin])
def test_methods_sum_to_one_three_way(fn):
    implied = [0.48, 0.30, 0.27]  # a soccer-style 1X2 with a 5% margin
    out = fn(implied)
    assert sum(out) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < p < 1 for p in out)


def test_spread_matches_fig3_right_column():
    assert round(method_spread([-1000, 600]) * 100, 1) == 3.1
    assert round(method_spread([-150, 130]) * 100, 1) == 0.4


def test_no_margin_is_identity():
    assert power([0.6, 0.4]) == pytest.approx([0.6, 0.4])
    assert devig_american([100, 100]) == pytest.approx([0.5, 0.5])


def test_odds_conversions():
    assert american_to_prob(-150) == pytest.approx(0.6)
    assert american_to_prob(130) == pytest.approx(100 / 230)
    assert american_to_decimal(-110) == pytest.approx(1.909090909)
    assert prob_to_american(0.6) == pytest.approx(-150)
    assert prob_to_american(0.4) == pytest.approx(150)
    with pytest.raises(ValueError):
        american_to_prob(50)
