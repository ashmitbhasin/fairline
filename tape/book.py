"""Local order-book state rebuilt from Kalshi snapshots and deltas, plus sequence-gap detection.

Kalshi books hold bids only. A YES bid at p is the same as a NO ask at 1 - p, so:
    best YES bid = highest YES bid
    best YES ask = 1 - highest NO bid
"""
from __future__ import annotations

from decimal import Decimal

ONE = Decimal("1")
ZERO = Decimal("0")


class SeqTracker:
    """Per-subscription (sid) sequence check. Any skipped number means the local book is wrong."""

    def __init__(self) -> None:
        self.last: dict[int, int] = {}

    def check(self, sid: int, seq: int) -> bool:
        """True if `seq` follows the previous message for `sid` (the first message always passes)."""
        prev = self.last.get(sid)
        self.last[sid] = seq
        return prev is None or seq == prev + 1

    def reset(self, sid: int) -> None:
        self.last.pop(sid, None)


class Book:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes: dict[Decimal, Decimal] = {}
        self.no: dict[Decimal, Decimal] = {}

    @staticmethod
    def _levels(levels) -> dict[Decimal, Decimal]:
        out = {}
        for price, qty in levels or []:
            q = Decimal(qty)
            if q > 0:
                out[Decimal(price)] = q
        return out

    def apply_snapshot(self, msg: dict) -> None:
        """Accepts the WebSocket shape (yes_dollars_fp/no_dollars_fp) or REST orderbook_fp (yes_dollars/no_dollars)."""
        self.yes = self._levels(msg.get("yes_dollars_fp", msg.get("yes_dollars")))
        self.no = self._levels(msg.get("no_dollars_fp", msg.get("no_dollars")))

    def apply_delta(self, msg: dict) -> None:
        side = self.yes if msg["side"] == "yes" else self.no
        price = Decimal(msg["price_dollars"])
        qty = side.get(price, ZERO) + Decimal(msg["delta_fp"])
        if qty > 0:
            side[price] = qty
        else:
            side.pop(price, None)

    def best_yes_bid(self) -> tuple[Decimal, Decimal] | None:
        if not self.yes:
            return None
        p = max(self.yes)
        return p, self.yes[p]

    def best_yes_ask(self) -> tuple[Decimal, Decimal] | None:
        if not self.no:
            return None
        p = max(self.no)
        return ONE - p, self.no[p]

    def mid(self) -> Decimal | None:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid[0] + ask[0]) / 2

    def top(self) -> dict:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        return {
            "ticker": self.ticker,
            "bid": str(bid[0]) if bid else None,
            "bid_qty": str(bid[1]) if bid else None,
            "ask": str(ask[0]) if ask else None,
            "ask_qty": str(ask[1]) if ask else None,
        }
