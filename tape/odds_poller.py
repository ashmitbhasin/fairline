"""Polls The Odds API for Pinnacle plus US books on an adaptive schedule.

    python -m tape.odds_poller --league nfl

Cost: credits = markets x regions per call. h2h + spreads + totals = 3 markets, and a list of
up to 10 bookmakers bills as one region, so every call costs 3 credits.
Schedule: every --fast seconds from --window-h hours before any kickoff until kickoff
(and through the game with --live), every --slow seconds otherwise.
Note: The Odds API says its Pinnacle odds come from Pinnacle's public website and may be delayed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

import httpx

from .config import DEFAULT_BOOKMAKERS, LEAGUES, ODDS_API_BASE, data_dir, odds_api_key
from .writer import JsonlWriter, now_ms

log = logging.getLogger("odds_poller")
MARKETS = "h2h,spreads,totals"
GAME_LENGTH = dt.timedelta(hours=3, minutes=30)


def next_interval(now: dt.datetime, kickoffs: list[dt.datetime], fast: float, slow: float, window: dt.timedelta, live: bool) -> float:
    for kickoff in kickoffs:
        start = kickoff - window
        end = kickoff + GAME_LENGTH if live else kickoff
        if start <= now <= end:
            return fast
    return slow


def kickoffs_from(events: list[dict]) -> list[dt.datetime]:
    out = []
    for e in events:
        ts = e.get("commence_time")
        if ts:
            out.append(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")))
    return out


async def run(args: argparse.Namespace) -> None:
    key = odds_api_key()
    if not key:
        raise SystemExit("No ODDS_API_KEY in .env. Sign up at the-odds-api.com and add it.")
    writer = JsonlWriter(data_dir(), "odds")
    window = dt.timedelta(hours=args.window_h)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            while True:
                kickoffs: list[dt.datetime] = []
                remaining = None
                for league in args.league:
                    sport = LEAGUES[league]["odds_sport"]
                    params = {
                        "apiKey": key,
                        "bookmakers": ",".join(args.bookmakers),
                        "markets": MARKETS,
                        "oddsFormat": "american",
                        "dateFormat": "iso",
                    }
                    sent = now_ms()
                    try:
                        resp = await client.get(f"{ODDS_API_BASE}/sports/{sport}/odds", params=params)
                    except httpx.TransportError as exc:
                        log.warning("%s request failed: %r", league, exc)
                        writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_poll_error", "league": league, "error": repr(exc)})
                        continue
                    recv = now_ms()
                    usage = {h: resp.headers.get(h) for h in ("x-requests-remaining", "x-requests-used", "x-requests-last")}
                    if resp.status_code != 200:
                        log.error("%s HTTP %s: %s", league, resp.status_code, resp.text[:300])
                        writer.write({"recv_ts_ms": recv, "src": "recorder", "type": "_http_error", "league": league, "status": resp.status_code, "body": resp.text[:500], "usage": usage})
                        continue
                    events = resp.json()
                    writer.write({"sent_ts_ms": sent, "recv_ts_ms": recv, "src": "odds_api", "league": league, "sport": sport, "usage": usage, "events": events})
                    kickoffs += kickoffs_from(events)
                    remaining = usage.get("x-requests-remaining")
                    log.info("%s: %d events, credits remaining %s", league, len(events), remaining)
                interval = next_interval(dt.datetime.now(dt.timezone.utc), kickoffs, args.fast, args.slow, window, args.live)
                if remaining is not None and float(remaining) < args.reserve:
                    log.warning("only %s credits left (reserve %d): slowing to %ds", remaining, args.reserve, args.slow)
                    interval = args.slow
                await asyncio.sleep(interval)
        finally:
            writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", nargs="+", default=["nfl"], choices=sorted(LEAGUES))
    parser.add_argument("--bookmakers", nargs="+", default=DEFAULT_BOOKMAKERS, help="up to 10 bill as one region")
    parser.add_argument("--fast", type=float, default=60.0, help="seconds between polls in the pregame window")
    parser.add_argument("--slow", type=float, default=900.0, help="seconds between polls otherwise")
    parser.add_argument("--window-h", type=float, default=3.0, help="hours before kickoff to poll fast")
    parser.add_argument("--live", action="store_true", help="keep polling fast during games (costs more credits)")
    parser.add_argument("--reserve", type=int, default=50, help="slow down when credits remaining drop below this")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
