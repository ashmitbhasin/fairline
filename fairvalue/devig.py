"""Remove the bookmaker's margin from a set of implied probabilities.

All functions take the implied probabilities of mutually exclusive, exhaustive outcomes
(margin included, so they sum to more than 1) and return fair probabilities that sum to 1.

    multiplicative  divide each by the total. Simple; least accurate on favorites.
    additive        subtract an equal share of the margin from each.
    power           raise each to the exponent k that makes them sum to 1.
    shin            Shin (1993) insider-trading model. On two-way markets it matches additive.

The spread between methods is a measure of model uncertainty: when power and multiplicative
disagree by more than the quoting margin, the desk should widen or stand down.
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

from .odds import american_to_prob


def _check(implied: Sequence[float]) -> None:
    if len(implied) < 2:
        raise ValueError("need at least two outcomes")
    if any(not 0 < q < 1 for q in implied):
        raise ValueError(f"implied probabilities must be in (0, 1): {implied}")


def _bisect(f: Callable[[float], float], lo: float, hi: float, iters: int = 200) -> float:
    """Root of a function that is decreasing on [lo, hi]."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def multiplicative(implied: Sequence[float]) -> list[float]:
    _check(implied)
    total = sum(implied)
    return [q / total for q in implied]


def additive(implied: Sequence[float]) -> list[float]:
    _check(implied)
    share = (sum(implied) - 1) / len(implied)
    return [q - share for q in implied]


def power(implied: Sequence[float]) -> list[float]:
    _check(implied)
    if abs(sum(implied) - 1) < 1e-12:
        return list(implied)
    # sum(q^k) decreases in k; k > 1 when there is a margin, < 1 if the set sums below 1.
    k = _bisect(lambda k: sum(q**k for q in implied) - 1, 0.01, 50.0)
    return [q**k for q in implied]


def shin(implied: Sequence[float]) -> list[float]:
    _check(implied)
    total = sum(implied)

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * q * q / total) - z) / (2 * (1 - z)) for q in implied]

    if total <= 1:
        return multiplicative(implied)
    z = _bisect(lambda z: sum(probs(z)) - 1, 0.0, 0.99)
    return probs(z)


METHODS: dict[str, Callable[[Sequence[float]], list[float]]] = {
    "multiplicative": multiplicative,
    "additive": additive,
    "power": power,
    "shin": shin,
}


def devig_american(prices: Sequence[float], method: str = "power") -> list[float]:
    """Fair probabilities from American prices, e.g. devig_american([-150, 130])."""
    return METHODS[method]([american_to_prob(p) for p in prices])


def all_methods(prices: Sequence[float]) -> dict[str, list[float]]:
    implied = [american_to_prob(p) for p in prices]
    return {name: fn(implied) for name, fn in METHODS.items()}


def method_spread(prices: Sequence[float], index: int = 0) -> float:
    """Power minus multiplicative for one outcome, in probability (0.0311 = 3.11 cents)."""
    implied = [american_to_prob(p) for p in prices]
    return power(implied)[index] - multiplicative(implied)[index]
