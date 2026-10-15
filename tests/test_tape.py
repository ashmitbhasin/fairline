import datetime as dt
import gzip
import json
import time
from decimal import Decimal

from tape.book import Book, SeqTracker
from tape.discover import event_date
from tape.odds_poller import next_interval
from tape.recorder_rest import in_live_window
from tape.writer import JsonlWriter

UTC = dt.timezone.utc


# --- order book -------------------------------------------------------------

SNAPSHOT = {  # WebSocket shape, from the Kalshi docs example
    "market_ticker": "KXNFLGAME-26OCT08TBDAL-DAL",
    "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
    "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
}


def test_snapshot_top_of_book():
    b = Book("T")
    b.apply_snapshot(SNAPSHOT)
    assert b.best_yes_bid() == (Decimal("0.2200"), Decimal("333.00"))
    assert b.best_yes_ask() == (Decimal("0.4400"), Decimal("146.00"))  # 1 - best NO bid 0.56
    assert b.mid() == Decimal("0.3300")


def test_delta_adds_and_removes_levels():
    b = Book("T")
    b.apply_snapshot(SNAPSHOT)
    b.apply_delta({"side": "yes", "price_dollars": "0.2300", "delta_fp": "10.00"})
    assert b.best_yes_bid() == (Decimal("0.2300"), Decimal("10.00"))
    b.apply_delta({"side": "yes", "price_dollars": "0.2300", "delta_fp": "-10.00"})
    assert b.best_yes_bid()[0] == Decimal("0.2200")
    b.apply_delta({"side": "no", "price_dollars": "0.5600", "delta_fp": "-146.00"})
    assert b.best_yes_ask()[0] == Decimal("0.4600")


def test_rest_shape_snapshot():
    b = Book("T")
    b.apply_snapshot({"yes_dollars": [["0.7900", "5.00"]], "no_dollars": [["0.2000", "7.00"]]})
    assert b.top() == {"ticker": "T", "bid": "0.7900", "bid_qty": "5.00", "ask": "0.8000", "ask_qty": "7.00"}


def test_empty_book():
    b = Book("T")
    assert b.mid() is None and b.best_yes_bid() is None and b.best_yes_ask() is None


def test_seq_tracker_detects_gaps_per_sid():
    s = SeqTracker()
    assert s.check(1, 5)
    assert s.check(1, 6)
    assert s.check(2, 1)
    assert not s.check(1, 8)  # 7 missing
    s.reset(1)
    assert s.check(1, 1)


# --- discovery --------------------------------------------------------------

def test_event_date_from_ticker():
    assert event_date("KXNFLGAME-26OCT08TBDAL") == dt.date(2026, 10, 8)
    assert event_date("KXNBAGAME-26OCT20BOSDET") == dt.date(2026, 10, 20)
    assert event_date("KXNFLWINS-CLE") is None
    assert event_date("KXNFLGAME-26FEB30XXYY") is None  # impossible date


# --- schedules --------------------------------------------------------------

def test_odds_interval_fast_only_in_pregame_window():
    kickoff = dt.datetime(2026, 10, 9, 0, 15, tzinfo=UTC)  # Thu 8:15 PM ET
    window = dt.timedelta(hours=3)
    assert next_interval(kickoff - dt.timedelta(hours=4), [kickoff], 60, 900, window, live=False) == 900
    assert next_interval(kickoff - dt.timedelta(hours=1), [kickoff], 60, 900, window, live=False) == 60
    assert next_interval(kickoff + dt.timedelta(hours=1), [kickoff], 60, 900, window, live=False) == 900
    assert next_interval(kickoff + dt.timedelta(hours=1), [kickoff], 60, 900, window, live=True) == 60


def test_rest_recorder_live_window():
    markets = [{"expected_expiration_time": "2026-10-09T03:15:00Z"}]
    assert in_live_window(markets, dt.datetime(2026, 10, 8, 21, 0, tzinfo=UTC))  # 5 PM ET pregame
    assert not in_live_window(markets, dt.datetime(2026, 10, 8, 15, 0, tzinfo=UTC))
    assert not in_live_window([{"expected_expiration_time": None}], dt.datetime(2026, 10, 8, 21, 0, tzinfo=UTC))


# --- writer -----------------------------------------------------------------

def test_writer_rotates_hourly_and_compresses(tmp_path):
    w = JsonlWriter(tmp_path, "test", flush_every_s=0)
    t0 = int(dt.datetime(2026, 10, 8, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    w.write({"recv_ts_ms": t0, "x": 1})
    w.write({"recv_ts_ms": t0 + 2000, "x": 2})  # next hour, next day
    w.close()
    first = tmp_path / "raw" / "test" / "2026-10-08" / "23.jsonl"
    second = tmp_path / "raw" / "test" / "2026-10-09" / "00.jsonl"
    assert json.loads(second.read_text())["x"] == 2
    gz = first.with_suffix(".jsonl.gz")
    for _ in range(50):  # compression runs on a background thread
        if gz.exists() and not first.exists():
            break
        time.sleep(0.05)
    assert json.loads(gzip.decompress(gz.read_bytes()))["x"] == 1


# --- outage handling ----------------------------------------------------------

class _DownREST:
    async def open_events(self, series):
        raise OSError("[Errno 8] nodename nor servname provided, or not known")


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, *args, **kwargs):
        self.warnings.append(args)


async def test_discovery_failure_keeps_previous_markets():
    from tape.discover import discover_or_keep

    previous = [{"ticker": "KXNFLGAME-26OCT08TBDAL-DAL"}]
    log = _Log()
    markets, refreshed = await discover_or_keep(_DownREST(), ["nfl"], 6, previous, log)
    assert markets == previous and refreshed is False
    assert log.warnings  # the outage is logged, not raised


async def test_sleep_wall_returns_by_the_wall_clock(monkeypatch):
    import tape.odds_poller as op

    clock = {"t": 1000.0}
    monkeypatch.setattr(op.time, "time", lambda: clock["t"])
    naps = []

    async def fake_sleep(s):
        naps.append(s)
        clock["t"] += 600 if len(naps) == 1 else s  # the Mac slept 10 min during the first nap

    monkeypatch.setattr(op.asyncio, "sleep", fake_sleep)
    await op.sleep_wall(900)
    assert naps[0] == 15 and sum(naps[1:]) <= 300 + 1e-9  # caught up instead of waiting a full 900 s more
