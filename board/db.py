"""SQLite storage for the board (data/board.db).

Tables
    games          one row per game: teams, kickoff (from the odds feed when matched)
    markets        one row per Kalshi market: kind (winner/spread/total), team, strike, label
    market_state   live row per market: top of book, last trade, today's volume, Pinnacle fair, rev
    depth          top 10 price levels per side, per market
    top_of_book    history: every change to best bid/ask
    trades         history: every public trade
    fair_history   history: every change to Pinnacle fair value
    tape           recent feed of price moves and trades (pruned)
    health         one row per recorder stream
    meta           schema version, last processed timestamp, rev counter

`rev` increases on every flush; the dashboard asks for rows with rev greater than the last one it saw.
WAL mode lets the server read while the indexer writes.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"
TAPE_KEEP = 3000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS games (
  key TEXT PRIMARY KEY, league TEXT, date TEXT, away TEXT, home TEXT, label TEXT,
  kickoff_ms INTEGER, kickoff_estimated INTEGER, odds_event_id TEXT);
CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY, league TEXT, series TEXT, event_ticker TEXT, game_key TEXT, kind TEXT,
  team TEXT, strike REAL, label TEXT, title TEXT, sort_key REAL);
CREATE TABLE IF NOT EXISTS market_state (
  ticker TEXT PRIMARY KEY, rev INTEGER NOT NULL, updated_ms INTEGER, source TEXT,
  bid REAL, bid_qty REAL, ask REAL, ask_qty REAL, mid REAL,
  last_price REAL, last_qty REAL, last_side TEXT, last_trade_ms INTEGER,
  trades_today INTEGER, volume_today REAL, day TEXT, open_mid REAL, fair REAL, fair_ms INTEGER);
CREATE INDEX IF NOT EXISTS market_state_rev ON market_state(rev);
CREATE TABLE IF NOT EXISTS depth (ticker TEXT PRIMARY KEY, rev INTEGER, levels TEXT);
CREATE TABLE IF NOT EXISTS top_of_book (ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, bid REAL, ask REAL, bid_qty REAL, ask_qty REAL);
CREATE INDEX IF NOT EXISTS top_of_book_ticker_ts ON top_of_book(ticker, ts_ms);
CREATE TABLE IF NOT EXISTS trades (ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, price REAL, qty REAL, taker_side TEXT);
CREATE INDEX IF NOT EXISTS trades_ticker_ts ON trades(ticker, ts_ms);
CREATE TABLE IF NOT EXISTS fair_history (ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, fair REAL);
CREATE INDEX IF NOT EXISTS fair_history_ticker_ts ON fair_history(ticker, ts_ms);
CREATE TABLE IF NOT EXISTS tape (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, ticker TEXT, kind TEXT,
  old REAL, new REAL, qty REAL, side TEXT);
CREATE TABLE IF NOT EXISTS health (
  stream TEXT PRIMARY KEY, updated_ms INTEGER, last_ms INTEGER, per_min INTEGER, running INTEGER,
  connected INTEGER, gaps_today INTEGER, disconnects_today INTEGER, credits TEXT, last_error TEXT);
"""


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if get_meta(conn, "schema_version") is None:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


class Writer:
    """Turns BoardState change queues into one transaction per flush."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.rev = int(get_meta(conn, "rev") or 0)
        self.flushes = 0

    def flush(self, state, drained: dict) -> int:
        self.rev += 1
        rev = self.rev
        c = self.conn
        with c:
            for key in drained["game_dirty"]:
                g = state.games[key]
                c.execute(
                    "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                    (g.key, g.league, g.date, g.away, g.home, g.label, g.kickoff_ms, int(g.kickoff_estimated), g.odds_event_id),
                )
            for ticker in drained["meta_dirty"]:
                m = state.metas[ticker]
                c.execute(
                    "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (m.ticker, m.league, m.series, m.event_ticker, m.game_key, m.kind, m.team, m.strike, m.label, m.title, m.sort_key),
                )
            # A new or relabelled market must reach the dashboard even if its prices did not change.
            for ticker in drained["dirty"] | (drained["meta_dirty"] & state.markets.keys()):
                s = state.markets[ticker]
                c.execute(
                    "INSERT OR REPLACE INTO market_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticker, rev, s.updated_ms, s.source, s.bid, s.bid_qty, s.ask, s.ask_qty, s.mid,
                     s.last_price, s.last_qty, s.last_side, s.last_trade_ms, s.trades_today, s.volume_today,
                     s.day, s.open_mid, s.fair, s.fair_ms),
                )
            for ticker in drained["depth_dirty"]:
                c.execute("INSERT OR REPLACE INTO depth VALUES (?,?,?)", (ticker, rev, json.dumps(state.depth(ticker))))
            if drained["tob"]:
                c.executemany("INSERT INTO top_of_book VALUES (?,?,?,?,?,?)", drained["tob"])
            if drained["trades"]:
                c.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", drained["trades"])
            if drained["fairs"]:
                c.executemany("INSERT INTO fair_history VALUES (?,?,?)", drained["fairs"])
            if drained["tape"]:
                c.executemany("INSERT INTO tape(ts_ms, ticker, kind, old, new, qty, side) VALUES (?,?,?,?,?,?,?)", drained["tape"])
            set_meta(c, "rev", rev)
            if state.latest_ms:
                # Never move back: a resume replays the last 45 minutes, and a checkpoint written from
                # inside that window would make the next restart insert the same history again.
                set_meta(c, "last_ts_ms", max(state.latest_ms, state.history_after_ms))
        self.flushes += 1
        if self.flushes % 40 == 0:
            with c:
                c.execute("DELETE FROM tape WHERE id <= (SELECT MAX(id) FROM tape) - ?", (TAPE_KEEP,))
        return rev

    def write_health(self, rows: list[tuple]) -> None:
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO health VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def load_catalog(conn: sqlite3.Connection, state) -> None:
    """Restore games and market metadata so a resumed indexer knows every market immediately."""
    from .state import Game, Meta

    for r in conn.execute("SELECT * FROM games"):
        state.games[r["key"]] = Game(r["key"], r["league"], r["date"], r["away"], r["home"], r["label"],
                                     r["kickoff_ms"], bool(r["kickoff_estimated"]), r["odds_event_id"])
    for r in conn.execute("SELECT * FROM markets"):
        state.metas[r["ticker"]] = Meta(r["ticker"], r["league"], r["series"], r["event_ticker"], r["game_key"], r["kind"],
                                        r["team"], r["strike"], r["label"], r["title"], r["sort_key"])


def load_today(conn: sqlite3.Connection, state, day: str, day_start_ms: int) -> None:
    """After a resume, take today's trade counts, volume and opening mid from the database, and each
    market's last trade where the replayed window held none (otherwise the next write would blank it)."""
    trades = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT ticker, COUNT(*), COALESCE(SUM(qty), 0) FROM trades WHERE ts_ms >= ? GROUP BY ticker", (day_start_ms,))}
    opens = {r[0]: r[1] for r in conn.execute(
        """SELECT t.ticker, (t.bid + t.ask) / 2 FROM top_of_book t
           JOIN (SELECT ticker, MIN(ts_ms) AS first FROM top_of_book WHERE ts_ms >= ? AND bid IS NOT NULL AND ask IS NOT NULL GROUP BY ticker) f
             ON t.ticker = f.ticker AND t.ts_ms = f.first""", (day_start_ms,))}
    last = {r["ticker"]: r for r in conn.execute(
        "SELECT ticker, last_price, last_qty, last_side, last_trade_ms FROM market_state WHERE last_trade_ms IS NOT NULL")}
    for ticker, st in state.markets.items():
        prev = last.get(ticker)
        if prev and (st.last_trade_ms is None or st.last_trade_ms < prev["last_trade_ms"]):
            st.last_price, st.last_qty, st.last_side, st.last_trade_ms = (
                prev["last_price"], prev["last_qty"], prev["last_side"], prev["last_trade_ms"])
        st.day = day
        st.trades_today, st.volume_today = trades.get(ticker, (0, 0.0))
        if ticker in opens:
            st.open_mid = round(opens[ticker], 4)
        state.dirty.add(ticker)
