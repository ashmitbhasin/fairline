"""Odds conversions between American, decimal and implied probability."""
from __future__ import annotations


def american_to_prob(odds: float) -> float:
    """Implied probability, margin included. -150 -> 0.6, +130 -> 0.4348."""
    if odds == 0 or -100 < odds < 100:
        raise ValueError(f"not a valid American price: {odds}")
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def american_to_decimal(odds: float) -> float:
    if odds == 0 or -100 < odds < 100:
        raise ValueError(f"not a valid American price: {odds}")
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


def prob_to_american(p: float) -> float:
    if not 0 < p < 1:
        raise ValueError(f"probability must be in (0, 1): {p}")
    return -100 * p / (1 - p) if p >= 0.5 else 100 * (1 - p) / p


def prob_to_decimal(p: float) -> float:
    if not 0 < p < 1:
        raise ValueError(f"probability must be in (0, 1): {p}")
    return 1 / p
