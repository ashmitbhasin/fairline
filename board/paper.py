"""Paper trading over the recorded tape: what Fairline's rules would have bet, and how it went.

Two rules run on every market with an identical Pinnacle line, from the first Pinnacle price to
kickoff, sampled once a minute, each at a fixed ladder of thresholds (decided before looking at
results, so the ladder shows where any edge appears rather than picking the best one afterwards):

    take    buy at the Kalshi ask (YES) or 1 - bid (NO) when devigged Pinnacle beats the price by at
            least the threshold after the taker fee. One bet per market.
    quote   rest a YES bid at fair - margin and a YES offer at fair + margin, never crossing the book
            (post-only). A quote fills only when a trade prints through it (G4's trade-through rule).
            At most one fill per side per market.

The headline variants are take at 2 cents and quote at 2 cents (PRIMARY).

Every bet is SIZE contracts (fewer when the book showed less), held to settlement and scored three ways:

    result    won, lost or tied (an NFL tie settles winner markets at 50 cents a side), and P&L in
              dollars after fees, from Kalshi's settled result
    CLV       Pinnacle's closing fair value minus the price paid incl. fee (the registered G4 metric)
    edge      fair value at entry minus the price paid incl. fee: what the bet was worth when placed

Winner bets use the home team's market only: the away team's market is the same bet mirrored.

No look-ahead, no stale prices:
    - a Pinnacle price counts from the moment the odds poller received it, not the bookmaker's own
      update time
    - a Pinnacle line the Odds API hadn't refreshed for PIN_STALE_MS when we received it counts as no
      price: that is how a line Pinnacle has pulled during news looks (seen live on 2026-10-15, CAR @ PHI:
      US books moved CAR from -115 to -180 while the Pinnacle price we received stayed at -111)
    - the rules act only on a price received in the last TRADE_FRESH_MS, which in practice means the
      3 hours before kickoff, when the poller runs every 2 minutes
    - minutes when the live recorder was down or silent are skipped, because the last book it saw may
      be stale, and so are minutes when the recorded book is crossed or locked (bid >= ask: an order
      book that can't exist on the exchange, so a missed update)

The same minute samples give the G2 gap measurement: |fair - Kalshi mid| beyond half the spread.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import math
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from fairvalue.fees import order_fee
from tape.config import KALSHI

from . import db
from .state import BoardState, iso_ms
from .tapefiles import stream_files

log = logging.getLogger("board.paper")
ET = ZoneInfo("America/New_York")
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS
SIZE = 100                    # contracts a bet (fewer when the book showed less)
TAKE_EDGES = (0.0, 0.005, 0.01, 0.02, 0.03)   # take: fair must beat the price by this much after the fee
QUOTE_MARGINS = (0.005, 0.01, 0.02, 0.03)     # quote: each quote sits this far from fair
PRIMARY = {"take": 0.02, "quote": 0.02}
GAP = 0.02                    # G2: a side-minute qualifies when |fair - mid| beats half the spread by this
FAIR_FRESH_MS = 35 * MIN_MS   # gap measurement: the poller runs at least every 30 min; older means it was down or asleep
TRADE_FRESH_MS = 5 * MIN_MS   # the rules only act on a Pinnacle price this recent
PIN_STALE_MS = 5 * MIN_MS     # a Pinnacle line not refreshed for this long when received is treated as pulled
PRICE_MIN, PRICE_MAX = 0.05, 0.95
MAKER_FEE_TYPE = "quadratic_with_maker_fees"  # every core NFL/NBA series as of 2026-10-06
FINAL_AFTER_MS = 15 * MIN_MS  # a game's pregame replay is final this long after kickoff
RULES = ("take", "quote")


def variant_id(rule: str, param: float) -> str:
    return f"{rule}@{param * 100:g}"


VARIANTS = [(variant_id("take", e), "take", e) for e in TAKE_EDGES] + [(variant_id("quote", q), "quote", q) for q in QUOTE_MARGINS]
ABS_BIN, ABS_BINS = 0.001, 200        # |fair - mid| histogram: 0.1 cent bins to 20 cents
BEYOND_LO, BEYOND_BIN, BEYOND_BINS = -0.05, 0.0025, 60   # gap beyond half spread: -5 to +10 cents
EPS = 1e-9


# ---------- Pinnacle as received ----------

class PinnacleTape:
    """Pinnacle's lines exactly as the odds poller received them, from data/raw/odds. Each hour file is
    parsed once and kept in memory until it changes (the live hour is re-read as it grows)."""

    def __init__(self, data: Path):
        self.data = Path(data)
        self.cache: dict[str, tuple[tuple, list]] = {}

    def polls(self) -> list[tuple[int, dict[str, dict]]]:
        """[(recv_ms, {odds_event_id: {market_key: market}})], oldest first."""
        out, seen = [], set()
        for _, path in sorted(stream_files(self.data, "odds").items()):
            rel = str(path)
            seen.add(rel)
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            hit = self.cache.get(rel)
            if not hit or hit[0] != sig:
                try:
                    hit = (sig, self._read(path))
                except (OSError, EOFError, gzip.BadGzipFile) as exc:  # being compressed right now
                    log.debug("skip %s: %r", rel, exc)
                    continue
                self.cache[rel] = hit
            out.extend(hit[1])
        self.cache = {k: v for k, v in self.cache.items() if k in seen}
        out.sort(key=lambda p: p[0])
        return out

    @staticmethod
    def _read(path: Path) -> list[tuple[int, dict]]:
        polls = []
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as fh:
            for line in fh:
                if b'"src":"odds_api"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # the live hour's last line may be half written
                events = {}
                for e in rec.get("events") or []:
                    pin = next((b for b in e.get("bookmakers") or [] if b.get("key") == "pinnacle"), None)
                    events[e.get("id")] = {m["key"]: m for m in (pin or {}).get("markets", []) if "key" in m}
                polls.append((rec.get("recv_ts_ms") or 0, events))
        return polls


def fair_series(polls: list, state: BoardState) -> dict[str, list[tuple[int, float | None]]]:
    """Devigged Pinnacle fair value per Kalshi market at every poll that carried its game, timestamped
    by receipt. None means Pinnacle had no identical line at that poll."""
    by_event: dict[str, list] = {}
    for meta in state.metas.values():
        game = state.games.get(meta.game_key)
        if game and game.odds_event_id:
            by_event.setdefault(game.odds_event_id, []).append(meta)
    out: dict[str, list] = {}
    for recv, events in polls:
        for event_id, markets in events.items():
            for meta in by_event.get(event_id, ()):
                fair, updated = state._fair(meta, markets, recv)
                if fair is not None and recv - updated > PIN_STALE_MS:
                    fair = None  # not refreshed: Pinnacle has most likely pulled the line
                out.setdefault(meta.ticker, []).append((recv, fair))
    return out


# ---------- the minute replay ----------

def _bet(variant: str, rule: str, m: dict, side: str, ts: int, price: float, qty: float, fee: float, fair_side: float) -> dict:
    return {"variant": variant, "rule": rule, "ticker": m["ticker"], "game_key": m["game_key"], "kind": m["kind"],
            "label": m["label"], "side": side, "ts_ms": ts, "kickoff_ms": m["kickoff_ms"], "price": round(price, 4),
            "qty": round(qty, 2), "fee": fee, "fair": round(fair_side, 5)}


def _take_candidates(fair: float, bid, ask, bid_qty, ask_qty) -> list[tuple]:
    """(edge after fee, side, price, qty, fee, fair of that side) for each side that could be bought.
    YES edge + NO edge = bid - ask - fees < 0, so at most one side is ever worth taking."""
    out = []
    for side, price, qty, fair_side in (("yes", ask, ask_qty, fair), ("no", None if bid is None else round(1 - bid, 4), bid_qty, 1 - fair)):
        if price is None or not (PRICE_MIN - EPS <= price <= PRICE_MAX + EPS):
            continue
        qty = min(SIZE, qty or 0)
        if qty < 1:
            continue
        fee = order_fee(price, qty, "taker")
        out.append((fair_side - price - fee / qty, side, price, qty, fee, fair_side))
    return out


def _quote_fills(variant: str, margin: float, m: dict, t0: int, fair: float, bid, ask, window: list, fills: dict) -> None:
    yes_bid = math.floor((fair - margin) * 100 + EPS) / 100
    if ask is not None:
        yes_bid = min(yes_bid, round(ask - 0.01, 2))  # post-only: never cross the offer
    yes_offer = math.ceil((fair + margin) * 100 - EPS) / 100
    if bid is not None:
        yes_offer = max(yes_offer, round(bid + 0.01, 2))
    # A trade whose taker sold YES (taker_side "no") below our bid would have hit us first; likewise a
    # YES buyer above our offer. Trading exactly at our price proves nothing about queue position.
    if fills["yes"] is None and PRICE_MIN - EPS <= yes_bid <= PRICE_MAX + EPS:
        through = sum(q for _, p, q, s in window if s == "no" and p is not None and p < yes_bid - EPS)
        if through > 0:
            qty = min(SIZE, through)
            fills["yes"] = _bet(variant, "quote", m, "yes", t0, yes_bid, qty, order_fee(yes_bid, qty, "maker", MAKER_FEE_TYPE), fair)
    no_price = round(1 - yes_offer, 2)
    if fills["no"] is None and PRICE_MIN - EPS <= no_price <= PRICE_MAX + EPS:
        through = sum(q for _, p, q, s in window if s == "yes" and p is not None and p > yes_offer + EPS)
        if through > 0:
            qty = min(SIZE, through)
            fills["no"] = _bet(variant, "quote", m, "no", t0, no_price, qty, order_fee(no_price, qty, "maker", MAKER_FEE_TYPE), 1 - fair)


class _Days:
    """ET calendar day of a timestamp, cached per hour (a datetime per minute would dominate the replay)."""

    def __init__(self):
        self.hours: dict[int, str] = {}

    def __call__(self, ts_ms: int) -> str:
        h = ts_ms // HOUR_MS
        day = self.hours.get(h)
        if day is None:
            day = self.hours[h] = dt.datetime.fromtimestamp(h * 3600, ET).date().isoformat()
        return day


def replay(m: dict, tob: list, trades: list, fairs: list, down: list, now: int, days: _Days | None = None) -> dict:
    """Walk one market minute by minute from its first Pinnacle price to kickoff.

    m       ticker, game_key, kind, label, kickoff_ms, trade (False: measure the gap only)
    tob     [(ts, bid, ask, bid_qty, ask_qty)] book changes, oldest first (one row before the window is enough)
    trades  [(ts, yes_price, qty, taker_side)], oldest first
    fairs   [(recv_ts, fair or None)], oldest first
    down    [(start, end)] sorted, non-overlapping: minutes overlapping these are skipped
    """
    days = days or _Days()
    takes: dict[str, dict | None] = {v: None for v, rule, _ in VARIANTS if rule == "take"}
    fills: dict[str, dict] = {v: {"yes": None, "no": None} for v, rule, _ in VARIANTS if rule == "quote"}
    take_open = [(v, e) for v, rule, e in VARIANTS if rule == "take"]
    quote_open = [(v, q) for v, rule, q in VARIANTS if rule == "quote"]
    kickoff = m["kickoff_ms"]
    out = {"ticker": m["ticker"], "game_key": m["game_key"], "kind": m["kind"], "kickoff_ms": kickoff, "bets": [],
           "gap": {"minutes": 0, "qualifying": 0, "abs_sum": 0.0, "days": {}, "abs_hist": {}, "beyond_hist": {}},
           "close_fair": None, "close_mid": None}
    before = [f for f in fairs if f[0] <= kickoff]
    if before:
        out["close_fair"] = before[-1][1]
    last_book = next((r for r in reversed(tob) if r[0] <= kickoff and r[1] is not None and r[2] is not None), None)
    if last_book:
        out["close_mid"] = round((last_book[1] + last_book[2]) / 2, 4)
    first_fair = next((t for t, f in fairs if f is not None), None)
    if first_fair is None or not tob:
        return out
    end = min(kickoff, now)
    start_min = max(first_fair, tob[0][0]) // MIN_MS + 1
    gap = out["gap"]
    ti = fi = xi = di = 0
    book = None
    fair = fair_ts = None
    for minute in range(start_min, -(-end // MIN_MS)):
        t0 = minute * MIN_MS
        t1 = min(t0 + MIN_MS, end)
        while ti < len(tob) and tob[ti][0] <= t0:
            book = tob[ti]
            ti += 1
        while fi < len(fairs) and fairs[fi][0] <= t0:
            fair_ts, fair = fairs[fi]
            fi += 1
        while xi < len(trades) and trades[xi][0] < t0:
            xi += 1
        xj = xi
        while xj < len(trades) and trades[xj][0] < t1:
            xj += 1
        window = trades[xi:xj]
        xi = xj
        while di < len(down) and down[di][1] <= t0:
            di += 1
        if di < len(down) and down[di][0] < t1:
            continue  # the recorder was blind for part of this minute
        if book is None or fair is None or t0 - fair_ts > FAIR_FRESH_MS:
            continue
        _, bid, ask, bid_qty, ask_qty = book
        if bid is not None and ask is not None and bid >= ask - EPS:
            continue  # crossed or locked: the recorder missed an update
        if bid is not None and ask is not None:
            g = abs(fair - (bid + ask) / 2)
            beyond = g - (ask - bid) / 2
            gap["minutes"] += 1
            gap["abs_sum"] += g
            q = beyond >= GAP - EPS
            gap["qualifying"] += q
            d = gap["days"].setdefault(days(t0), [0, 0])
            d[0] += 1
            d[1] += q
            ab = min(ABS_BINS, int(g / ABS_BIN + EPS))
            gap["abs_hist"][ab] = gap["abs_hist"].get(ab, 0) + 1
            bb = max(0, min(BEYOND_BINS, int(math.floor((beyond - BEYOND_LO) / BEYOND_BIN + EPS))))
            gap["beyond_hist"][bb] = gap["beyond_hist"].get(bb, 0) + 1
        if not m.get("trade") or not (PRICE_MIN <= fair <= PRICE_MAX) or t0 - fair_ts > TRADE_FRESH_MS:
            continue
        if take_open:
            cands = _take_candidates(fair, bid, ask, bid_qty, ask_qty)
            best = max(cands, default=None)
            if best is not None:
                edge, side, price, qty, fee, fair_side = best
                for v, threshold in take_open:
                    if edge >= threshold - EPS:
                        takes[v] = _bet(v, "take", m, side, t0, price, qty, fee, fair_side)
                take_open = [(v, e) for v, e in take_open if takes[v] is None]
        if quote_open and window:
            for v, margin in quote_open:
                _quote_fills(v, margin, m, t0, fair, bid, ask, window, fills[v])
            quote_open = [(v, q) for v, q in quote_open if fills[v]["yes"] is None or fills[v]["no"] is None]
    out["bets"] = [b for b in takes.values() if b] + [b for f in fills.values() for b in (f["yes"], f["no"]) if b]
    return out


# ---------- settlement ----------

RESOLVED = {"determined", "amended", "finalized", "settled"}
FINAL = {"finalized", "settled"}


def fetch_markets(tickers: list[str]) -> list[dict]:
    """Kalshi's public market records (no key needed)."""
    resp = httpx.get(f'{KALSHI["prod"]["rest"]}/markets', params={"tickers": ",".join(tickers), "limit": len(tickers)}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("markets") or []


class Settlements:
    """Kalshi's results for markets the paper book bet on, cached in data/tracker/settlements.json.
    A finalized result never changes, so it is fetched once; others are re-checked every 10 minutes."""

    RECHECK_MS = 10 * MIN_MS
    BATCH = 50

    def __init__(self, path: Path, fetch=fetch_markets):
        self.path = Path(path)
        self.fetch = fetch
        self.checked: dict[str, int] = {}
        self.error: str | None = None
        try:
            self.items: dict[str, dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.items = {}

    def update(self, tickers: list[str], now: int) -> None:
        due = sorted(t for t in set(tickers)
                     if (self.items.get(t) or {}).get("status") not in FINAL and now - self.checked.get(t, 0) >= self.RECHECK_MS)
        changed = False
        for i in range(0, len(due), self.BATCH):
            chunk = due[i:i + self.BATCH]
            try:
                markets = self.fetch(chunk)
            except Exception as exc:  # offline or Kalshi down: keep what we have, try again next time
                self.error = f"Kalshi results unavailable: {exc!r}"[:300]
                log.warning("%s", self.error)
                break
            for mk in markets:
                ticker = mk.get("ticker")
                if ticker not in chunk:
                    continue
                rec = {"status": mk.get("status"), "result": mk.get("result") or None, "settled_ms": iso_ms(mk.get("settlement_ts")),
                       "value": _float(mk.get("settlement_value_dollars"))}
                if rec != self.items.get(ticker):
                    self.items[ticker] = rec
                    changed = True
            for t in chunk:
                self.checked[t] = now
        else:
            self.error = None
        if changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=0, sort_keys=True))
            os.replace(tmp, self.path)

    def result(self, ticker: str) -> tuple[float | None, str | None, int | None]:
        """(what one YES contract paid, "yes" | "no" | "scalar" | "void" | None, settled_ms)."""
        it = self.items.get(ticker) or {}
        result = it.get("result")
        if it.get("status") in RESOLVED:
            if result == "yes":
                return 1.0, result, it.get("settled_ms")
            if result == "no":
                return 0.0, result, it.get("settled_ms")
            if result == "scalar" and it.get("value") is not None:
                return it["value"], result, it.get("settled_ms")
        if it.get("status") in FINAL:
            return None, "void", it.get("settled_ms")
        return None, None, None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- scoring ----------

def score(bet: dict, yes_paid: float | None, result: str | None, settled_ms: int | None, close_fair: float | None, now: int) -> dict:
    qty, price, fee = bet["qty"], bet["price"], bet["fee"]
    per_fee = fee / qty
    b = dict(bet)
    b["cost"] = round(qty * price + fee, 2)
    b["edge"] = round(bet["fair"] - price - per_fee, 5)
    b["ev"] = round(qty * b["edge"], 2)
    b["close"] = None
    b["clv"] = None
    if close_fair is not None and now >= bet["kickoff_ms"]:
        b["close"] = round(close_fair if bet["side"] == "yes" else 1 - close_fair, 5)
        b["clv"] = round(b["close"] - price - per_fee, 5)
    b["result"], b["settled_ms"], b["pnl"] = result, settled_ms, None
    if yes_paid is not None:
        paid = yes_paid if bet["side"] == "yes" else 1 - yes_paid
        b["status"] = "tie" if result == "scalar" else "won" if result == bet["side"] else "lost"
        b["pnl"] = round(qty * (paid - price) - fee, 2)
    elif result == "void":
        b["status"], b["pnl"] = "void", 0.0
    else:
        b["status"] = "open" if now < bet["kickoff_ms"] else "live"
    return b


def _ci95(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return 1.96 * math.sqrt(var / len(xs))


SETTLED = ("won", "lost", "tie")


def tally(bets: list[dict]) -> dict:
    settled = [b for b in bets if b["status"] in SETTLED]
    won = sum(1 for b in settled if b["status"] == "won")
    lost = sum(1 for b in settled if b["status"] == "lost")
    staked = sum(b["cost"] for b in settled)
    pnl = sum(b["pnl"] for b in settled)
    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    return {
        "bets": len(bets), "settled": len(settled), "won": won, "lost": lost, "tied": len(settled) - won - lost,
        "void": sum(1 for b in bets if b["status"] == "void"),
        "pending": sum(1 for b in bets if b["status"] in ("open", "live")),
        "win_pct": won / (won + lost) if won + lost else None,
        "pnl": round(pnl, 2), "staked": round(staked, 2), "roi": pnl / staked if staked else None,
        "ev": round(sum(b["ev"] for b in settled), 2), "ev_all": round(sum(b["ev"] for b in bets), 2),
        "edge_mean": sum(b["edge"] for b in bets) / len(bets) if bets else None,
        "clv_n": len(clvs), "clv_mean": sum(clvs) / len(clvs) if clvs else None, "clv_ci": _ci95(clvs),
        "beat_close": sum(1 for c in clvs if c > EPS) / len(clvs) if clvs else None,
    }


SPLITS = {
    "kind": ("Market", lambda b: b["kind"], ["winner", "spread", "total"],
             {"winner": "Winner", "spread": "Spread", "total": "Total"}),
    "side": ("Side", lambda b: b["side"], ["yes", "no"], {"yes": "Bought YES", "no": "Bought NO"}),
    "edge": ("Edge at entry", lambda b: "lt0" if b["edge"] < 0 else "0-2" if b["edge"] < 0.02 else "2-4" if b["edge"] < 0.04 else "4+",
             ["lt0", "0-2", "2-4", "4+"], {"lt0": "Below 0¢", "0-2": "0–2¢", "2-4": "2–4¢", "4+": "4¢ or more"}),
    "lead": ("Placed", lambda b: "lt3h" if b["kickoff_ms"] - b["ts_ms"] < 3 * HOUR_MS else "lt24h" if b["kickoff_ms"] - b["ts_ms"] < 24 * HOUR_MS else "1d",
             ["1d", "lt24h", "lt3h"], {"1d": "A day or more out", "lt24h": "3–24 h out", "lt3h": "Under 3 h out"}),
    "price": ("Price", lambda b: "fav" if b["price"] >= 0.5 else "dog", ["fav", "dog"], {"fav": "50¢ or more", "dog": "Under 50¢"}),
}


def summarize(bets: list[dict]) -> dict:
    out = tally(bets)
    settled = sorted((b for b in bets if b["status"] in SETTLED), key=lambda b: (b["settled_ms"] or b["kickoff_ms"], b["ts_ms"]))
    out["form"] = [{"won": "W", "lost": "L", "tie": "T"}[b["status"]] for b in settled[-20:]]
    curve, cum, cum_ev = [], 0.0, 0.0
    for b in settled:
        cum += b["pnl"]
        cum_ev += b["ev"]
        curve.append([b["settled_ms"] or b["kickoff_ms"], round(cum, 2), round(cum_ev, 2)])
    out["curve"] = curve
    out["splits"] = {}
    for name, (title, key, order, labels) in SPLITS.items():
        rows = []
        for k in order:
            group = [b for b in bets if key(b) == k]
            if group:
                rows.append({"key": k, "label": labels[k], **tally(group)})
        out["splits"][name] = {"title": title, "rows": rows}
    return out


# ---------- the book ----------

class PaperBook:
    """Replays every matched market, keeps finished games' replays in memory, scores the bets."""

    def __init__(self, data: Path, db_path: Path, settlements: Settlements):
        self.data = Path(data)
        self.db_path = Path(db_path)
        self.tape = PinnacleTape(self.data)
        self.settlements = settlements
        self.final: dict[str, dict] = {}  # ticker -> replay, for games over before the blind spots were last measured
        self.days = _Days()

    def run(self, down: list, down_known_until: int, now: int) -> dict:
        if not self.db_path.exists():
            return {"available": False, "reason": "no board database yet"}
        conn = db.connect(self.db_path, readonly=True)
        try:
            state = BoardState()
            db.load_catalog(conn, state)
            fairs = fair_series(self.tape.polls(), state)
            replays = []
            for ticker, series in fairs.items():
                if not any(f is not None for _, f in series):
                    continue
                meta = state.metas.get(ticker)
                game = state.games.get(meta.game_key) if meta else None
                if not game or not game.kickoff_ms or meta.kind not in ("winner", "spread", "total"):
                    continue
                kickoff = game.kickoff_ms
                cached = self.final.get(ticker)
                if cached and cached["kickoff_ms"] == kickoff:
                    replays.append(cached)
                    continue
                m = {"ticker": ticker, "game_key": game.key, "kind": meta.kind, "label": meta.label, "kickoff_ms": kickoff,
                     "trade": meta.kind != "winner" or meta.team == game.home}
                first = next(t for t, f in series if f is not None)
                prior = conn.execute("SELECT ts_ms, bid, ask, bid_qty, ask_qty FROM top_of_book WHERE ticker = ? AND ts_ms < ? "
                                     "ORDER BY ts_ms DESC LIMIT 1", (ticker, first)).fetchall()
                tob = [tuple(r) for r in prior] + [tuple(r) for r in conn.execute(
                    "SELECT ts_ms, bid, ask, bid_qty, ask_qty FROM top_of_book WHERE ticker = ? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
                    (ticker, first, kickoff))]
                trades = [tuple(r) for r in conn.execute(
                    "SELECT ts_ms, price, qty, taker_side FROM trades WHERE ticker = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
                    (ticker, first, kickoff))]
                r = replay(m, tob, trades, series, down, now, self.days)
                if kickoff + FINAL_AFTER_MS < min(now, down_known_until):
                    self.final[ticker] = r
                replays.append(r)
            return self._score(replays, state, now)
        finally:
            conn.close()

    def _score(self, replays: list[dict], state: BoardState, now: int) -> dict:
        raw = [b for r in replays for b in r["bets"]]
        self.settlements.update([b["ticker"] for b in raw if b["kickoff_ms"] <= now], now)
        close = {r["ticker"]: r["close_fair"] for r in replays}
        bets = []
        for b in raw:
            yes_paid, result, settled_ms = self.settlements.result(b["ticker"])
            bets.append(score(b, yes_paid, result, settled_ms, close.get(b["ticker"]), now))
        bets.sort(key=lambda b: b["ts_ms"])
        return {
            "available": True,
            "params": {"size": SIZE, "price_min": PRICE_MIN, "price_max": PRICE_MAX, "fair_fresh_min": FAIR_FRESH_MS // MIN_MS,
                       "trade_fresh_min": TRADE_FRESH_MS // MIN_MS, "pin_stale_min": PIN_STALE_MS // MIN_MS},
            "variants": [{"id": v, "rule": rule, "param": p, "primary": PRIMARY[rule] == p,
                          **summarize([b for b in bets if b["variant"] == v])} for v, rule, p in VARIANTS],
            "bets": bets,
            "games": self._games(replays, bets, state, now),
            "g2": g2_summary(replays, now),
            "settlement_error": self.settlements.error,
        }

    @staticmethod
    def _games(replays: list[dict], bets: list[dict], state: BoardState, now: int) -> list[dict]:
        by_game: dict[str, dict] = {}
        for r in replays:
            g = state.games[r["game_key"]]
            row = by_game.setdefault(g.key, {"key": g.key, "league": g.league, "label": g.label, "kickoff_ms": g.kickoff_ms,
                                             "home": g.home, "matched": 0, "close": None, "variants": {}})
            row["matched"] += 1
            meta = state.metas.get(r["ticker"])
            if meta and meta.kind == "winner" and meta.team == g.home and r["close_fair"] is not None and r["close_mid"] is not None:
                row["close"] = {"fair": r["close_fair"], "mid": r["close_mid"]}
        for row in by_game.values():
            mine = [b for b in bets if b["game_key"] == row["key"]]
            for v, _, _ in VARIANTS:
                row["variants"][v] = tally([b for b in mine if b["variant"] == v])
            row["status"] = "upcoming" if now < row["kickoff_ms"] else "played"
        return sorted(by_game.values(), key=lambda r: -r["kickoff_ms"])


def g2_summary(replays: list[dict], now: int, recent_days: int = 7) -> dict:
    total = qual = 0
    abs_sum = 0.0
    days: dict[str, list[int]] = {}
    abs_hist = [0] * (ABS_BINS + 1)
    beyond_hist = [0] * (BEYOND_BINS + 1)
    by_kind: dict[str, list[int]] = {}
    markets = 0
    for r in replays:
        g = r["gap"]
        if not g["minutes"]:
            continue
        markets += 1
        total += g["minutes"]
        qual += g["qualifying"]
        abs_sum += g["abs_sum"]
        k = by_kind.setdefault(r["kind"], [0, 0])
        k[0] += g["minutes"]
        k[1] += g["qualifying"]
        for d, (n, q) in g["days"].items():
            row = days.setdefault(d, [0, 0])
            row[0] += n
            row[1] += q
        for i, n in g["abs_hist"].items():
            abs_hist[int(i)] += n
        for i, n in g["beyond_hist"].items():
            beyond_hist[int(i)] += n
    if not total:
        return {"available": False, "reason": "no pregame minutes with a fresh Pinnacle price yet"}

    def quantile(q: float) -> float:
        target, run = q * total, 0
        for i, n in enumerate(abs_hist):
            run += n
            if run >= target:
                return (i + 0.5) * ABS_BIN
        return ABS_BINS * ABS_BIN

    cutoff = dt.datetime.fromtimestamp((now - recent_days * 86_400_000) / 1000, ET).date().isoformat()
    recent = sum(q for d, (_, q) in days.items() if d > cutoff)
    return {
        "available": True, "markets": markets, "side_minutes": total, "qualifying_minutes": qual,
        "qualifying_hours": round(qual / 60, 1), "recent_hours": round(recent / 60, 1), "recent_days": recent_days,
        "median_gap": quantile(0.5), "p90_gap": quantile(0.9), "mean_abs_gap": abs_sum / total,
        "by_kind": {k: {"minutes": v[0], "qualifying": v[1]} for k, v in by_kind.items()},
        "days": [{"day": d, "minutes": n, "qualifying": q} for d, (n, q) in sorted(days.items())],
        "beyond_hist": {"lo": BEYOND_LO, "bin": BEYOND_BIN, "counts": beyond_hist},
    }
