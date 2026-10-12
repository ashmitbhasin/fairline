"""Fallback recorder: polls Kalshi's public order books. No API key needed.

    python -m tape.recorder_rest --league nfl --days 6

Use it until the WebSocket recorder has credentials. It sees the book every few seconds instead of
every change, so it can't measure fast convergence (gate G3), but it does capture depth and spreads.

Polling speeds up near games: every --fast seconds from 4 hours before an expected game end until
30 minutes after it (game end comes from Kalshi's expected_expiration_time), every --slow seconds
otherwise. A book is written only when it changed since the last poll.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import time

from .config import LEAGUES, data_dir
from .discover import discover_or_keep
from .kalshi_rest import KalshiREST
from .writer import JsonlWriter, now_ms

log = logging.getLogger("recorder_rest")
BATCH = 50
LIVE_BEFORE_END = dt.timedelta(hours=7)  # ~4h pregame + ~3h of game before the expected end
LIVE_AFTER_END = dt.timedelta(minutes=30)


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def in_live_window(markets: list[dict], now: dt.datetime) -> bool:
    for m in markets:
        end = _parse_ts(m.get("expected_expiration_time"))
        if end and end - LIVE_BEFORE_END <= now <= end + LIVE_AFTER_END:
            return True
    return False


async def run(args: argparse.Namespace) -> None:
    rest = KalshiREST(args.env, requests_per_s=args.rps)
    writer = JsonlWriter(data_dir(), "kalshi_rest")
    last_hash: dict[str, str] = {}
    markets: list[dict] = []
    next_discovery = 0.0
    try:
        while True:
            if time.monotonic() >= next_discovery:
                found, refreshed = await discover_or_keep(rest, args.league, args.days, markets, log)
                if refreshed:
                    markets = found
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_markets", "markets": markets})
                    log.info("tracking %d markets", len(markets))
                    next_discovery = time.monotonic() + args.refresh_min * 60
                else:
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_discovery_error"})
                    next_discovery = time.monotonic() + 60
            tickers = sorted({m["ticker"] for m in markets})
            cycle_start = time.monotonic()
            written = 0
            for i in range(0, len(tickers), BATCH):
                try:
                    books = await rest.orderbooks(tickers[i : i + BATCH])
                except Exception as exc:  # keep recording other batches; the gap shows in the tape
                    log.warning("batch %d failed: %r", i // BATCH, exc)
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_poll_error", "error": repr(exc)})
                    continue
                recv = now_ms()
                for b in books:
                    body = json.dumps(b.get("orderbook_fp", {}), sort_keys=True)
                    digest = hashlib.sha1(body.encode()).hexdigest()
                    if last_hash.get(b["ticker"]) == digest:
                        continue
                    last_hash[b["ticker"]] = digest
                    writer.write({"recv_ts_ms": recv, "src": "kalshi_rest", "type": "orderbook", "ticker": b["ticker"], "book": b.get("orderbook_fp", {})})
                    written += 1
            live = in_live_window(markets, dt.datetime.now(dt.timezone.utc))
            interval = args.fast if live else args.slow
            log.info("cycle: %d markets, %d changed books, %s mode", len(tickers), written, "fast" if live else "slow")
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - cycle_start)))
    finally:
        writer.close()
        await rest.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", nargs="+", default=["nfl"], choices=sorted(LEAGUES))
    parser.add_argument("--days", type=int, default=6)
    parser.add_argument("--env", default="prod", choices=["prod", "demo"])
    parser.add_argument("--fast", type=float, default=5.0, help="seconds between polls near games")
    parser.add_argument("--slow", type=float, default=60.0, help="seconds between polls otherwise")
    parser.add_argument("--rps", type=float, default=10.0, help="max REST requests per second")
    parser.add_argument("--refresh-min", type=float, default=30.0, help="re-discover markets this often")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
