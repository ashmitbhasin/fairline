"""Serves the board dashboard and a read-only JSON API over data/board.db.

    python -m board.server            # http://127.0.0.1:8765
    python -m board.server --open     # and open it in your browser

Endpoints (all GET, JSON):
    /api/snapshot                       everything the dashboard needs to draw
    /api/updates?rev=N&tape=M           rows changed since rev N, tape events after id M
    /api/history?ticker=T&range=6h      top of book, trades and fair value over 1h/6h/24h/all
    /api/depth?ticker=T                 top 10 price levels per side
    /api/sparks?tickers=A,B&range=24h   compact mid-price series for sparklines

Tracker (what the code produces: paper record, live stats, history; see board/tracker.py):
    GET  /api/tracker                   everything the Tracker tab draws
    POST /api/tracker/refresh           recompute now instead of at the next 5-minute tick
The POST requires the X-Fairline header and a same-origin request, so another website open in your
browser cannot trigger it.

Listens on 127.0.0.1 only: the data never leaves this machine (Kalshi's Developer Agreement §3.1
bars redistributing it).
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import sqlite3
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tape.config import data_dir

from . import db
from .tracker import Tracker

log = logging.getLogger("board.server")
STATIC = Path(__file__).resolve().parent / "static"
RANGES = {"15m": 15 * 60_000, "1h": 3600_000, "6h": 6 * 3600_000, "24h": 24 * 3600_000, "all": None}
MAX_POINTS = 1500
MARKET_COLUMNS = (
    "m.ticker, m.league, m.game_key, m.kind, m.team, m.strike, m.label, m.title, m.sort_key, "
    "s.rev, s.updated_ms, s.source, s.bid, s.bid_qty, s.ask, s.ask_qty, s.mid, s.last_price, s.last_qty, "
    "s.last_side, s.last_trade_ms, s.trades_today, s.volume_today, s.open_mid, s.fair, s.fair_ms"
)


def now_ms() -> int:
    return int(time.time() * 1000)


class Board:
    """Read-only queries. A fresh connection per request keeps threads independent and picks up rebuilds."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def conn(self):
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        c = db.connect(self.path, readonly=True)
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def _rows(cur) -> list[dict]:
        return [dict(r) for r in cur]

    def _common(self, c) -> dict:
        return {
            "server_ms": now_ms(),
            "rev": int(db.get_meta(c, "rev") or 0),
            "last_ts_ms": int(db.get_meta(c, "last_ts_ms") or 0),
            "games": self._rows(c.execute("SELECT * FROM games ORDER BY kickoff_ms IS NULL, kickoff_ms, key")),
            "health": self._rows(c.execute("SELECT * FROM health ORDER BY stream")),
        }

    def _tape(self, c, after_id: int = 0, limit: int = 150) -> list[dict]:
        return self._rows(c.execute(
            "SELECT t.id, t.ts_ms, t.ticker, t.kind, t.old, t.new, t.qty, t.side, m.label, m.game_key, m.kind AS market_kind "
            "FROM tape t LEFT JOIN markets m USING (ticker) WHERE t.id > ? ORDER BY t.id DESC LIMIT ?", (after_id, limit)))

    def snapshot(self) -> dict:
        with self.conn() as c:
            out = self._common(c)
            out["markets"] = self._rows(c.execute(f"SELECT {MARKET_COLUMNS} FROM markets m LEFT JOIN market_state s USING (ticker)"))
            out["tape"] = self._tape(c)
            return out

    def updates(self, rev: int, tape_id: int) -> dict:
        with self.conn() as c:
            out = self._common(c)
            if rev > out["rev"]:
                return {"resync": True, **out}
            out["markets"] = self._rows(c.execute(
                f"SELECT {MARKET_COLUMNS} FROM market_state s JOIN markets m USING (ticker) WHERE s.rev > ?", (rev,)))
            out["tape"] = self._tape(c, tape_id, 200)
            return out

    def history(self, ticker: str, range_key: str) -> dict:
        span = RANGES.get(range_key, RANGES["6h"])
        now = now_ms()
        with self.conn() as c:
            last = int(db.get_meta(c, "last_ts_ms") or now)
            end = max(now, last)
            cutoff = end - span if span else 0
            rows = c.execute("SELECT ts_ms, bid, ask FROM top_of_book WHERE ticker = ? AND ts_ms >= ? ORDER BY ts_ms", (ticker, cutoff)).fetchall()
            tob = [[r[0], r[1], r[2]] for r in rows]
            trades = [list(r) for r in c.execute(
                "SELECT ts_ms, price, qty, taker_side FROM trades WHERE ticker = ? AND ts_ms >= ? ORDER BY ts_ms DESC LIMIT 4000", (ticker, cutoff))][::-1]
            fair = [list(r) for r in c.execute("SELECT ts_ms, fair FROM fair_history WHERE ticker = ? AND ts_ms >= ? ORDER BY ts_ms", (ticker, cutoff))]
            if span:
                # Carry the state from just before the window so lines start at its left edge.
                before = c.execute("SELECT bid, ask FROM top_of_book WHERE ticker = ? AND ts_ms < ? ORDER BY ts_ms DESC LIMIT 1", (ticker, cutoff)).fetchone()
                if before:
                    tob.insert(0, [cutoff, before[0], before[1]])
                fair_before = c.execute("SELECT fair FROM fair_history WHERE ticker = ? AND ts_ms < ? ORDER BY ts_ms DESC LIMIT 1", (ticker, cutoff)).fetchone()
                if fair_before:
                    fair.insert(0, [cutoff, fair_before[0]])
            else:
                firsts = [series[0][0] for series in (tob, trades, fair) if series]
                cutoff = min(firsts) if firsts else end - 3600_000
            return {"ticker": ticker, "from_ms": cutoff, "to_ms": end, "tob": downsample(tob, MAX_POINTS), "trades": trades, "fair": fair}

    def depth(self, ticker: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT rev, levels FROM depth WHERE ticker = ?", (ticker,)).fetchone()
            return {"ticker": ticker, "rev": row["rev"] if row else None, **(json.loads(row["levels"]) if row else {"bids": [], "asks": []})}

    def sparks(self, tickers: list[str], range_key: str) -> dict:
        span = RANGES.get(range_key) or RANGES["24h"]
        cutoff = now_ms() - span
        out = {}
        with self.conn() as c:
            for t in tickers[:80]:
                rows = c.execute("SELECT ts_ms, (bid + ask) / 2 FROM top_of_book WHERE ticker = ? AND ts_ms >= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (t, cutoff)).fetchall()
                out[t] = downsample([[r[0], r[1]] for r in rows], 48)
        return {"range": range_key, "series": out}


def downsample(points: list[list], limit: int) -> list[list]:
    """Keep the last point in each of `limit` equal time buckets (plus the very first point)."""
    if len(points) <= limit:
        return points
    start, end = points[0][0], points[-1][0]
    width = max(1, (end - start) / limit)
    kept: dict[int, list] = {}
    for p in points:
        kept[int((p[0] - start) // width)] = p
    return [points[0]] + [kept[k] for k in sorted(kept) if kept[k] is not points[0]]


class Handler(BaseHTTPRequestHandler):
    board: Board
    tracker: Tracker | None = None
    server_version = "FairlineBoard/1.0"

    def log_message(self, fmt, *args):  # keep the terminal quiet; errors are logged explicitly
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(), "application/json")

    def do_GET(self):  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/snapshot":
                return self._json(self.board.snapshot())
            if url.path == "/api/updates":
                return self._json(self.board.updates(int(q.get("rev", 0)), int(q.get("tape", 0))))
            if url.path == "/api/history":
                return self._json(self.board.history(q["ticker"], q.get("range", "6h")))
            if url.path == "/api/depth":
                return self._json(self.board.depth(q["ticker"]))
            if url.path == "/api/sparks":
                tickers = [t for t in q.get("tickers", "").split(",") if t]
                return self._json(self.board.sparks(tickers, q.get("range", "24h")))
            if url.path == "/api/tracker":
                if not self.tracker:
                    raise FileNotFoundError("tracker")
                return self._json(self.tracker.snapshot())
            return self._static(url.path)
        except FileNotFoundError:
            return self._json({"error": "no_database", "detail": "board.db does not exist yet. Start the indexer: python -m board.indexer"}, 503)
        except (KeyError, ValueError) as exc:
            return self._json({"error": "bad_request", "detail": repr(exc)}, 400)
        except sqlite3.OperationalError as exc:
            log.warning("database busy or rebuilding: %r", exc)
            return self._json({"error": "database_unavailable", "detail": str(exc)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as exc:
            log.exception("unhandled error on %s", self.path)
            return self._json({"error": "internal", "detail": repr(exc)}, 500)

    # ---------- tracker ----------
    def _same_origin(self) -> bool:
        """Block cross-site requests: browsers always send Origin on POST, and a custom header forces a
        CORS preflight this server never approves."""
        if self.headers.get("X-Fairline") != "1":
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host', '')}"

    def do_POST(self):  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        try:
            if url.path != "/api/tracker/refresh":
                return self._json({"error": "not_found"}, 404)
            if not self._same_origin():
                return self._json({"error": "forbidden", "detail": "cross-origin or missing X-Fairline header"}, 403)
            started = self.tracker.request_refresh() if self.tracker else False
            return self._json({"started": started, "refreshing": bool(self.tracker and self.tracker.refreshing)})
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as exc:
            log.exception("unhandled error on POST %s", self.path)
            return self._json({"error": "internal", "detail": repr(exc)}, 500)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Handler.board = Board(data_dir() / "board.db")
    Handler.tracker = Tracker(data_dir(), data_dir() / "board.db")
    Handler.tracker.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    log.info("board at %s (Ctrl+C to stop)", url)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
