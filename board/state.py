"""In-memory board model built from tape records.

Feed it records in time order (apply_ws / apply_rest / apply_odds). It keeps every order book,
the latest trade, today's volume and the Pinnacle fair value per market, and queues what changed
for the database writer to drain.

Fair value matches Pinnacle only where the line is identical:
    winner  "DAL"        <- Pinnacle h2h Dallas Cowboys
    spread  "DAL -9.5"   <- Pinnacle spreads Dallas Cowboys -9.5   (Kalshi "DAL wins by over 9.5")
    total   "Over 47.5"  <- Pinnacle totals Over 47.5
Pinnacle's two-way prices are devigged with the power method. A Kalshi strike with no identical
Pinnacle line has no fair value (whole-number Pinnacle lines include pushes, so they never match).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from fairvalue.devig import power
from fairvalue.odds import american_to_prob
from tape.book import Book
from tape.discover import event_date

from .teams import full_name, split_game_code

ET = ZoneInfo("America/New_York")
KIND_ORDER = {"winner": 0, "spread": 1, "total": 2}
GAME_LENGTH_MS = {"nfl": 3 * 3600_000, "nba": 2 * 3600_000 + 30 * 60_000}
WS_QUIET_MS = 20_000  # REST snapshots only update a book when the WebSocket has been silent this long
MINUS = "−"
_STRIKE = re.compile(r"over\s+([0-9]+(?:\.[0-9]+)?)", re.I)
_LEADING_LETTERS = re.compile(r"^[A-Z]+")
_TRAILING_DIGITS = re.compile(r"([0-9]+)$")


def et_day(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, ET).date().isoformat()


def iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def kind_of(series: str) -> str | None:
    for suffix, kind in (("GAME", "winner"), ("SPREAD", "spread"), ("TOTAL", "total")):
        if series.endswith(suffix):
            return kind
    return None


def league_of(series: str) -> str | None:
    for league in ("nfl", "nba"):
        if series.upper().startswith("KX" + league.upper()):
            return league
    return None


@dataclass
class Meta:
    ticker: str
    league: str
    series: str
    event_ticker: str
    game_key: str
    kind: str
    team: str | None
    strike: float | None
    label: str
    title: str
    sort_key: float
    inferred: bool = False


@dataclass
class Game:
    key: str
    league: str
    date: str
    away: str | None
    home: str | None
    label: str
    kickoff_ms: int | None = None
    kickoff_estimated: bool = True
    odds_event_id: str | None = None


@dataclass
class MarketState:
    book: Book
    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    mid: float | None = None
    last_price: float | None = None
    last_qty: float | None = None
    last_side: str | None = None
    last_trade_ms: int | None = None
    trades_today: int = 0
    volume_today: float = 0.0
    day: str | None = None
    open_mid: float | None = None
    fair: float | None = None
    fair_ms: int | None = None
    updated_ms: int | None = None
    source: str | None = None


@dataclass
class StreamHealth:
    last_ms: int | None = None
    per_second: dict[int, int] = field(default_factory=dict)
    connected: bool | None = None
    gaps_today: int = 0
    disconnects_today: int = 0
    day: str | None = None
    credits: str | None = None
    last_error: str | None = None

    def tick(self, ts_ms: int, n: int = 1) -> None:
        self.last_ms = max(self.last_ms or 0, ts_ms)
        sec = ts_ms // 1000
        self.per_second[sec] = self.per_second.get(sec, 0) + n
        if len(self.per_second) > 180:
            for old in sorted(self.per_second)[:-120]:
                del self.per_second[old]

    def per_minute(self, now_ms: int) -> int:
        cutoff = now_ms // 1000 - 60
        return sum(n for sec, n in self.per_second.items() if sec > cutoff)

    def roll(self, ts_ms: int) -> None:
        day = et_day(ts_ms)
        if day != self.day:
            self.day, self.gaps_today, self.disconnects_today = day, 0, 0


class BoardState:
    def __init__(self, history_after_ms: int = 0):
        self.metas: dict[str, Meta] = {}
        self.games: dict[str, Game] = {}
        self.markets: dict[str, MarketState] = {}
        self.health = {s: StreamHealth() for s in ("kalshi_ws", "kalshi_rest", "odds")}
        self.ws_last_ms = 0
        # Records at or before this time rebuild state but are not re-queued as history
        # (used when resuming over a window the database already holds).
        self.history_after_ms = history_after_ms
        self.latest_ms = 0
        self._recv = 0  # receive time of the record being applied
        self._reset_queues()

    def _reset_queues(self) -> None:
        self.dirty: set[str] = set()
        self.depth_dirty: set[str] = set()
        self.meta_dirty: set[str] = set()
        self.game_dirty: set[str] = set()
        self.tob_rows: list[tuple] = []
        self.trade_rows: list[tuple] = []
        self.fair_rows: list[tuple] = []
        self.tape_rows: list[tuple] = []

    def drain(self) -> dict:
        out = {
            "dirty": self.dirty, "depth_dirty": self.depth_dirty, "meta_dirty": self.meta_dirty,
            "game_dirty": self.game_dirty, "tob": self.tob_rows, "trades": self.trade_rows,
            "fairs": self.fair_rows, "tape": self.tape_rows,
        }
        self._reset_queues()
        return out

    def _history(self) -> bool:
        # Decided by receive time, the same clock as the stored checkpoint (last_ts_ms), so a resume
        # neither repeats nor skips rows at the boundary even when event times lag arrival.
        return self._recv > self.history_after_ms

    # ---- market metadata -------------------------------------------------------------

    def _meta_from(self, ticker: str, series: str, event_ticker: str, league: str, sub_title: str | None, inferred: bool) -> Meta | None:
        kind = kind_of(series)
        if kind is None:
            return None
        game_key = event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker
        suffix = ticker.rsplit("-", 1)[1]
        team, strike = None, None
        if kind == "winner":
            team = suffix
        else:
            match = _STRIKE.search(sub_title or "")
            if match:
                strike = float(match.group(1))
            else:  # infer from the ticker: DAL10 -> over 9.5, 48 -> over 47.5
                digits = _TRAILING_DIGITS.search(suffix)
                strike = int(digits.group(1)) - 0.5 if digits else None
            if kind == "spread":
                letters = _LEADING_LETTERS.match(suffix)
                team = letters.group(0) if letters else None
        game = self._ensure_game(game_key, league, event_ticker)
        away_first = 0 if team == game.away else 1
        if kind == "winner":
            label, sort_key = team or suffix, float(away_first)
        elif kind == "spread":
            label = f"{team} {MINUS}{strike:g}" if strike is not None else suffix
            sort_key = away_first * 1000 + (strike or 0)
        else:
            label = f"Over {strike:g}" if strike is not None else suffix
            sort_key = strike or 0
        return Meta(ticker, league, series, event_ticker, game_key, kind, team, strike, label, sub_title or label, sort_key, inferred)

    def _ensure_game(self, game_key: str, league: str, event_ticker: str) -> Game:
        game = self.games.get(game_key)
        if game is None:
            day = event_date(event_ticker)
            teams = split_game_code(league, game_key[7:]) if len(game_key) > 7 else None
            away, home = teams if teams else (None, None)
            label = f"{away} @ {home}" if teams else game_key[7:]
            game = Game(game_key, league, day.isoformat() if day else "", away, home, label)
            self.games[game_key] = game
            self.game_dirty.add(game_key)
        return game

    def ingest_markets(self, entries: list[dict]) -> None:
        for e in entries:
            ticker = e.get("ticker")
            if not ticker or not e.get("series"):
                continue
            meta = self._meta_from(ticker, e["series"], e["event_ticker"], e.get("league") or league_of(e["series"]) or "", e.get("yes_sub_title"), inferred=False)
            if meta is None:
                continue
            game = self.games[meta.game_key]
            end_ms = iso_ms(e.get("expected_expiration_time"))
            if game.kickoff_estimated and end_ms:
                estimate = end_ms - GAME_LENGTH_MS.get(meta.league, 3 * 3600_000)
                if game.kickoff_ms != estimate:
                    game.kickoff_ms = estimate
                    self.game_dirty.add(game.key)
            old = self.metas.get(ticker)
            if old != meta:
                self.metas[ticker] = meta
                self.meta_dirty.add(ticker)

    def _ensure_market(self, ticker: str) -> MarketState:
        st = self.markets.get(ticker)
        if st is None:
            st = MarketState(book=Book(ticker))
            self.markets[ticker] = st
            if ticker not in self.metas:
                parts = ticker.split("-")
                if len(parts) >= 3:
                    series, event_ticker = parts[0], "-".join(parts[:2])
                    league = league_of(series)
                    meta = self._meta_from(ticker, series, event_ticker, league or "", None, inferred=True) if league else None
                    if meta:
                        self.metas[ticker] = meta
                        self.meta_dirty.add(ticker)
        return st

    # ---- books and trades -------------------------------------------------------------

    def _roll_day(self, st: MarketState, ts_ms: int) -> None:
        day = et_day(ts_ms)
        if st.day != day:
            st.day, st.trades_today, st.volume_today, st.open_mid = day, 0, 0.0, None

    def _after_book(self, ticker: str, st: MarketState, ts_ms: int, source: str) -> None:
        self.depth_dirty.add(ticker)
        bid, ask = st.book.best_yes_bid(), st.book.best_yes_ask()
        top = (
            float(bid[0]) if bid else None, float(bid[1]) if bid else None,
            float(ask[0]) if ask else None, float(ask[1]) if ask else None,
        )
        if top == (st.bid, st.bid_qty, st.ask, st.ask_qty):
            return
        old_mid = st.mid
        st.bid, st.bid_qty, st.ask, st.ask_qty = top
        st.mid = round((st.bid + st.ask) / 2, 4) if st.bid is not None and st.ask is not None else None
        st.updated_ms, st.source = ts_ms, source
        self._roll_day(st, ts_ms)
        if st.open_mid is None and st.mid is not None:
            st.open_mid = st.mid
        self.dirty.add(ticker)
        if self._history():
            self.tob_rows.append((ticker, ts_ms, st.bid, st.ask, st.bid_qty, st.ask_qty))
            if st.mid is not None and old_mid is not None and abs(st.mid - old_mid) >= 0.0049:
                self.tape_rows.append((ts_ms, ticker, "move", old_mid, st.mid, None, None))

    def apply_ws(self, rec: dict) -> None:
        recv = self._recv = rec.get("recv_ts_ms") or 0
        self.latest_ms = max(self.latest_ms, recv)
        h = self.health["kalshi_ws"]
        h.roll(recv)
        if rec.get("src") == "recorder":
            kind = rec.get("type")
            if kind == "_markets":
                self.ingest_markets(rec.get("markets") or [])
            elif kind == "_connected":
                h.connected, h.last_error = True, None
            elif kind == "_disconnected":
                h.connected, h.last_error = False, rec.get("error")
                h.disconnects_today += 1
            elif kind == "_gap":
                h.gaps_today += 1
            return
        m = rec.get("m") or {}
        kind = m.get("type")
        msg = m.get("msg") or {}
        if kind in ("orderbook_snapshot", "orderbook_delta", "trade"):
            h.tick(recv)
            h.connected = True
            self.ws_last_ms = recv
        if kind == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            if ticker:
                st = self._ensure_market(ticker)
                st.book.apply_snapshot(msg)
                self._after_book(ticker, st, m.get("sending_ts_ms") or recv, "ws")
        elif kind == "orderbook_delta":
            ticker = msg.get("market_ticker")
            if ticker:
                st = self._ensure_market(ticker)
                st.book.apply_delta(msg)
                self._after_book(ticker, st, msg.get("ts_ms") or recv, "ws")
        elif kind == "trade":
            self._apply_trade(msg, recv)

    def _apply_trade(self, msg: dict, recv: int) -> None:
        ticker = msg.get("market_ticker")
        if not ticker:
            return
        st = self._ensure_market(ticker)
        ts = msg.get("ts_ms") or recv
        price = float(msg.get("yes_price_dollars") or 0) or None
        qty = float(msg.get("count_fp") or 0)
        side = msg.get("taker_side")
        self._roll_day(st, ts)
        st.last_price, st.last_qty, st.last_side, st.last_trade_ms = price, qty, side, ts
        st.trades_today += 1
        st.volume_today += qty
        self.dirty.add(ticker)
        if self._history():
            self.trade_rows.append((ticker, ts, price, qty, side))
            self.tape_rows.append((ts, ticker, "trade", None, price, qty, side))

    def apply_rest(self, rec: dict) -> None:
        recv = self._recv = rec.get("recv_ts_ms") or 0
        self.latest_ms = max(self.latest_ms, recv)
        h = self.health["kalshi_rest"]
        if rec.get("src") == "recorder":
            if rec.get("type") == "_markets":
                self.ingest_markets(rec.get("markets") or [])
            elif rec.get("type") in ("_poll_error", "_discovery_error"):
                h.last_error = rec.get("error") or rec.get("type")
            return
        if rec.get("type") != "orderbook":
            return
        h.tick(recv)
        h.last_error = None
        ticker = rec.get("ticker")
        if not ticker or recv - self.ws_last_ms <= WS_QUIET_MS:
            return  # the WebSocket is live and authoritative
        st = self._ensure_market(ticker)
        st.book.apply_snapshot(rec.get("book") or {})
        self._after_book(ticker, st, recv, "rest")

    # ---- odds and fair value ---------------------------------------------------------

    def apply_odds(self, rec: dict) -> None:
        recv = self._recv = rec.get("recv_ts_ms") or 0
        self.latest_ms = max(self.latest_ms, recv)
        h = self.health["odds"]
        if rec.get("src") != "odds_api":
            if rec.get("type") in ("_http_error", "_poll_error", "_bad_response"):
                h.last_error = rec.get("error") or f'HTTP {rec.get("status")}'
            return
        h.tick(recv)
        h.last_error = None
        h.credits = (rec.get("usage") or {}).get("x-requests-remaining") or h.credits
        league = rec.get("league") or ""
        index = {}
        for game in self.games.values():
            if game.league == league and game.away and game.home:
                names = frozenset({full_name(league, game.away), full_name(league, game.home)})
                index[(game.date, names)] = game
        for event in rec.get("events") or []:
            commence = iso_ms(event.get("commence_time"))
            if not commence:
                continue
            game = index.get((et_day(commence), frozenset({event.get("home_team"), event.get("away_team")})))
            if game is None:
                continue
            if game.kickoff_ms != commence or game.kickoff_estimated or game.odds_event_id != event.get("id"):
                game.kickoff_ms, game.kickoff_estimated, game.odds_event_id = commence, False, event.get("id")
                self.game_dirty.add(game.key)
            pin = next((b for b in event.get("bookmakers") or [] if b.get("key") == "pinnacle"), None)
            markets = {m["key"]: m for m in (pin or {}).get("markets", [])}
            for ticker, meta in self.metas.items():
                if meta.game_key != game.key:
                    continue
                fair, fair_ms = self._fair(meta, markets, recv)
                self._set_fair(ticker, fair, fair_ms)

    def _fair(self, meta: Meta, markets: dict, recv: int) -> tuple[float | None, int]:
        key = {"winner": "h2h", "spread": "spreads", "total": "totals"}[meta.kind]
        market = markets.get(key)
        if not market:
            return None, recv
        outcomes = market.get("outcomes") or []
        when = iso_ms(market.get("last_update")) or recv
        try:
            implied = [american_to_prob(o["price"]) for o in outcomes]
        except (KeyError, ValueError, TypeError):
            return None, when
        if len(implied) < 2:
            return None, when
        target = None
        if meta.kind == "winner":
            name = full_name(meta.league, meta.team)
            target = next((i for i, o in enumerate(outcomes) if o.get("name") == name), None)
        elif meta.kind == "spread" and meta.strike is not None:
            name = full_name(meta.league, meta.team)
            target = next((i for i, o in enumerate(outcomes) if o.get("name") == name and o.get("point") is not None and abs(o["point"] + meta.strike) < 1e-9), None)
        elif meta.kind == "total" and meta.strike is not None:
            target = next((i for i, o in enumerate(outcomes) if o.get("name") == "Over" and o.get("point") is not None and abs(o["point"] - meta.strike) < 1e-9), None)
        if target is None:
            return None, when
        return round(power(implied)[target], 5), when

    def _set_fair(self, ticker: str, fair: float | None, fair_ms: int) -> None:
        st = self._ensure_market(ticker)
        if st.fair == fair and (fair is None or st.fair_ms == fair_ms):
            return
        st.fair, st.fair_ms = fair, fair_ms
        self.dirty.add(ticker)
        if self._history():
            self.fair_rows.append((ticker, fair_ms, fair))

    # ---- views -----------------------------------------------------------------------

    def depth(self, ticker: str, levels: int = 10) -> dict:
        book = self.markets[ticker].book
        bids = sorted(book.yes.items(), reverse=True)[:levels]
        asks = sorted(((1 - p, q) for p, q in book.no.items()))[:levels]
        return {"bids": [[float(p), float(q)] for p, q in bids], "asks": [[float(p), float(q)] for p, q in asks]}
