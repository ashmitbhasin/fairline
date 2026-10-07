"""Kalshi trading fees, break-even and sizing.

    taker fee = 0.07   x multiplier x C x P x (1 - P), rounded up to the cent per order
    maker fee = 0.0175 x multiplier x C x P x (1 - P), only on series whose fee_type is
                "quadratic_with_maker_fees"; zero on plain "quadratic" series

Read fee_type and fee_multiplier from GET /series/{ticker} before quoting. As of 2026-10-06 the
API lists every core NFL, NBA, college football and NHL game/spread/total series as
quadratic_with_maker_fees with multiplier 1, so makers pay 0.44c per contract at 50c there.
The 0.07 and 0.0175 coefficients come from Kalshi's fee schedule as reproduced by several
third-party guides; confirm against the official schedule and the demo environment.
"""
from __future__ import annotations

import math

TAKER_COEF = 0.07
MAKER_COEF = 0.0175
MAKER_FEE_TYPES = {"quadratic_with_maker_fees"}


def fee_coefficient(role: str, fee_type: str = "quadratic", multiplier: float = 1.0) -> float:
    if role == "taker":
        return TAKER_COEF * multiplier
    if role == "maker":
        return MAKER_COEF * multiplier if fee_type in MAKER_FEE_TYPES else 0.0
    raise ValueError(f"role must be 'maker' or 'taker': {role}")


def order_fee(price: float, contracts: int, role: str, fee_type: str = "quadratic", multiplier: float = 1.0) -> float:
    """Fee in dollars for one order of `contracts` at `price` (dollars), rounded up to the cent."""
    if not 0 < price < 1:
        raise ValueError(f"price must be in (0, 1) dollars: {price}")
    raw = fee_coefficient(role, fee_type, multiplier) * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 9)) / 100


def fee_per_contract(price: float, contracts: int, role: str, fee_type: str = "quadratic", multiplier: float = 1.0) -> float:
    return order_fee(price, contracts, role, fee_type, multiplier) / contracts


def break_even(price: float, fee_per_contract_: float) -> float:
    """Win probability needed to break even buying YES at `price` with that fee."""
    return price + fee_per_contract_


def kelly_fraction(fair: float, price: float, fee_per_contract_: float = 0.0, fair_error: float = 0.0, fraction: float = 0.25) -> float:
    """Fractional Kelly stake (share of bankroll) for buying YES.

    Uses the standard binary-contract Kelly f* = (q - c) / (1 - c), where c = price + fee and
    q = fair - fair_error (size to the edge you can defend at one standard error).
    """
    cost = price + fee_per_contract_
    q = fair - fair_error
    if cost >= 1 or q <= cost:
        return 0.0
    return fraction * (q - cost) / (1 - cost)
