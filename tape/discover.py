"""Find the Kalshi markets to record: core game, spread and total markets for games in a date window.

    python -m tape.discover --league nfl --days 6
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

from .config import LEAGUES, data_dir
from .kalshi_rest import KalshiREST

ET = ZoneInfo("America/New_York")
_EVENT_DATE = re.compile(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
TRADEABLE_STATUSES = {"active", "open"}


def event_date(event_ticker: str) -> dt.date | None:
    """Game date encoded in the event ticker, e.g. KXNFLGAME-26OCT08TBDAL -> 2026-10-08."""
    match = _EVENT_DATE.search(event_ticker)
    if not match:
        return None
    yy, mon, dd = match.groups()
    try:
        return dt.date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


def today_et() -> dt.date:
    return dt.datetime.now(ET).date()


async def discover(rest: KalshiREST, leagues: list[str], start: dt.date, days: int) -> list[dict]:
    """All open core markets for games dated start..start+days (ET), one dict per market."""
    end = start + dt.timedelta(days=days)
    found: list[dict] = []
    for league in leagues:
        for series in LEAGUES[league]["kalshi_series"]:
            for event in await rest.open_events(series):
                game_date = event_date(event["event_ticker"])
                if game_date is None or not (start <= game_date <= end):
                    continue
                for market in await rest.markets_for_event(event["event_ticker"]):
                    if market.get("status") not in TRADEABLE_STATUSES:
                        continue
                    found.append(
                        {
                            "league": league,
                            "series": series,
                            "event_ticker": event["event_ticker"],
                            "ticker": market["ticker"],
                            "game_date": game_date.isoformat(),
                            "title": market.get("title"),
                            "yes_sub_title": market.get("yes_sub_title"),
                            "expected_expiration_time": market.get("expected_expiration_time"),
                        }
                    )
    return found


def save_market_list(markets: list[dict]) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = data_dir() / "meta" / f"markets_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(markets, indent=1))
    return str(path)


async def _main(args: argparse.Namespace) -> None:
    rest = KalshiREST(args.env)
    try:
        markets = await discover(rest, args.league, today_et(), args.days)
    finally:
        await rest.close()
    by_event: dict[str, int] = {}
    for m in markets:
        game = f'{m["league"].upper()} {m["event_ticker"].split("-", 1)[1]}'
        by_event[game] = by_event.get(game, 0) + 1
    for game, n in sorted(by_event.items(), key=lambda kv: kv[0].split(" ")[1]):
        print(f"{game:32s} {n:4d} markets")
    print(f"total: {len(markets)} markets across {len(by_event)} games")
    if args.save:
        print("saved:", save_market_list(markets))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", nargs="+", default=["nfl"], choices=sorted(LEAGUES))
    parser.add_argument("--days", type=int, default=6, help="look-ahead window in days (default 6)")
    parser.add_argument("--env", default="prod", choices=["prod", "demo"])
    parser.add_argument("--save", action="store_true", help="write the list to data/meta/")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
