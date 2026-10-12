"""Board: tape following, state rebuilding, fair-value matching, storage and the HTTP API.

Records use the exact shapes the recorders write (observed live on 2026-10-07/08).
"""
import gzip
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from board import db
from board.server import Board, Handler, downsample
from board.state import BoardState, iso_ms
from board.tapefiles import StreamTailer, stream_files
from board.teams import full_name, short_name, split_game_code
from fairvalue.devig import power
from fairvalue.odds import american_to_prob

T0 = 1791395000000  # 2026-10-07 13:43 ET
DAL, TB = "KXNFLGAME-26OCT08TBDAL-DAL", "KXNFLGAME-26OCT08TBDAL-TB"
DAL_SPREAD, TOTAL = "KXNFLSPREAD-26OCT08TBDAL-DAL8", "KXNFLTOTAL-26OCT08TBDAL-48"


def markets_record(ts=T0):
    def m(series, ticker, sub):
        return {"league": "nfl", "series": series, "event_ticker": f"{series}-26OCT08TBDAL", "ticker": ticker,
                "game_date": "2026-10-08", "title": sub, "yes_sub_title": sub, "expected_expiration_time": "2026-10-09T03:15:00Z"}
    return {"recv_ts_ms": ts, "src": "recorder", "type": "_markets", "markets": [
        m("KXNFLGAME", DAL, "Dallas"), m("KXNFLGAME", TB, "Tampa Bay"),
        m("KXNFLSPREAD", DAL_SPREAD, "DAL Cowboys wins by over 7.5 points"), m("KXNFLTOTAL", TOTAL, "Over 47.5 points")]}


def ws(ts, kind, msg, seq=1, sid=1):
    return {"recv_ts_ms": ts, "src": "kalshi_ws", "m": {"type": kind, "sid": sid, "seq": seq, "msg": msg, "sending_ts_ms": ts}}


def snapshot(ts, ticker=DAL, yes=(("0.7900", "100.00"),), no=(("0.1900", "50.00"),)):
    return ws(ts, "orderbook_snapshot", {"market_ticker": ticker, "yes_dollars_fp": [list(x) for x in yes], "no_dollars_fp": [list(x) for x in no]})


def odds_record(ts, spread_point=-7.5):
    return {"recv_ts_ms": ts, "src": "odds_api", "league": "nfl", "usage": {"x-requests-remaining": "470"}, "events": [{
        "id": "e1", "commence_time": "2026-10-09T00:15:00Z", "home_team": "Dallas Cowboys", "away_team": "Tampa Bay Buccaneers",
        "bookmakers": [{"key": "draftkings", "markets": []}, {"key": "pinnacle", "markets": [
            {"key": "h2h", "last_update": "2026-10-07T17:34:10Z", "outcomes": [{"name": "Dallas Cowboys", "price": -444}, {"name": "Tampa Bay Buccaneers", "price": 352}]},
            {"key": "spreads", "outcomes": [{"name": "Dallas Cowboys", "price": -102, "point": spread_point}, {"name": "Tampa Bay Buccaneers", "price": -110, "point": -spread_point}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": -109, "point": 47.5}, {"name": "Under", "price": -105, "point": 47.5}]},
        ]}]}]}


def devig(*prices):
    return round(power([american_to_prob(p) for p in prices])[0], 5)


# --- teams -------------------------------------------------------------------------

def test_teams():
    assert split_game_code("nfl", "TBDAL") == ("TB", "DAL")
    assert split_game_code("nfl", "NENYJ") == ("NE", "NYJ")
    assert split_game_code("nba", "LACLAL") == ("LAC", "LAL")
    assert split_game_code("nfl", "XXYYZ") is None
    assert full_name("nfl", "JAX") == "Jacksonville Jaguars"  # alias
    assert full_name("nfl", None) is None
    assert short_name("nba", "POR") == "Trail Blazers"


# --- tape following ----------------------------------------------------------------

def _hour(root, stream, day, hh):
    path = root / "raw" / stream / day / f"{hh}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_tailer_holds_partial_lines_and_follows_rotation(tmp_path):
    a = _hour(tmp_path, "s", "2026-10-07", "17")
    a.write_text('{"n":1}\n{"n":2}\n{"n":')
    t = StreamTailer(tmp_path, "s")
    assert [r["n"] for r in t.poll()] == [1, 2]
    with a.open("a") as fh:
        fh.write('3}\n')
    assert [r["n"] for r in t.poll()] == [3]
    _hour(tmp_path, "s", "2026-10-07", "18").write_text('{"n":4}\n')
    assert [r["n"] for r in t.poll()] == [4]
    assert t.poll() == []


def test_tailer_finishes_an_hour_that_was_compressed_while_open(tmp_path):
    a = _hour(tmp_path, "s", "2026-10-07", "17")
    a.write_text('{"n":1}\n')
    t = StreamTailer(tmp_path, "s")
    assert [r["n"] for r in t.poll()] == [1]
    with a.open("a") as fh:  # the recorder's last writes before rotating
        fh.write('{"n":2}\n')
    # The recorder rotates and its compressor replaces 17.jsonl with 17.jsonl.gz.
    _hour(tmp_path, "s", "2026-10-07", "18").write_text('{"n":3}\n')
    a.with_suffix(".jsonl.gz").write_bytes(gzip.compress(a.read_bytes()))
    a.unlink()
    assert [r["n"] for r in t.poll()] == [2, 3]


def test_plain_file_wins_while_compression_is_in_progress(tmp_path):
    a = _hour(tmp_path, "s", "2026-10-07", "17")
    a.write_text('{"n":1}\n')
    a.with_suffix(".jsonl.gz").write_bytes(b"partial")
    assert stream_files(tmp_path, "s")[("2026-10-07", "17")].name == "17.jsonl"


# --- state -------------------------------------------------------------------------

def test_markets_ingest_builds_games_labels_and_kickoff_estimate():
    s = BoardState()
    s.ingest_markets(markets_record()["markets"])
    g = s.games["26OCT08TBDAL"]
    assert (g.away, g.home, g.label, g.date) == ("TB", "DAL", "TB @ DAL", "2026-10-08")
    assert g.kickoff_estimated and g.kickoff_ms == iso_ms("2026-10-09T03:15:00Z") - 3 * 3600_000  # expected end minus 3 h
    assert s.metas[DAL_SPREAD].label == "DAL −7.5" and s.metas[DAL_SPREAD].strike == 7.5
    assert s.metas[TOTAL].label == "Over 47.5"


def test_book_trade_and_history_flow():
    s = BoardState()
    s.apply_ws(markets_record())
    s.apply_ws(snapshot(T0 + 1000))
    st = s.markets[DAL]
    assert (st.bid, st.ask, st.mid) == (0.79, 0.81, 0.8)
    s.apply_ws(ws(T0 + 2000, "orderbook_delta", {"market_ticker": DAL, "price_dollars": "0.8000", "delta_fp": "10.00", "side": "yes", "ts_ms": T0 + 1990}, seq=2))
    assert (st.bid, st.bid_qty, st.mid) == (0.80, 10.0, 0.805)
    s.apply_ws(ws(T0 + 3000, "trade", {"market_ticker": DAL, "yes_price_dollars": "0.8100", "count_fp": "12.00", "taker_side": "yes", "ts_ms": T0 + 2990}, sid=2))
    assert (st.last_price, st.trades_today, st.volume_today) == (0.81, 1, 12.0)
    out = s.drain()
    assert [r[1] for r in out["tob"]] == [T0 + 1000, T0 + 1990]
    assert out["trades"] == [(DAL, T0 + 2990, 0.81, 12.0, "yes")]
    assert [e[2] for e in out["tape"]] == ["move", "trade"]
    assert DAL in out["dirty"] and DAL in out["depth_dirty"]
    assert s.depth(DAL) == {"bids": [[0.8, 10.0], [0.79, 100.0]], "asks": [[0.81, 50.0]]}


def test_unchanged_top_of_book_writes_no_history():
    s = BoardState()
    s.apply_ws(snapshot(T0))
    s.drain()
    s.apply_ws(ws(T0 + 1000, "orderbook_delta", {"market_ticker": DAL, "price_dollars": "0.5000", "delta_fp": "5.00", "side": "yes"}, seq=2))
    out = s.drain()
    assert out["tob"] == [] and DAL in out["depth_dirty"] and DAL not in out["dirty"]


def test_rest_snapshots_only_apply_when_websocket_is_quiet():
    s = BoardState()
    s.apply_ws(snapshot(T0))
    rest = {"recv_ts_ms": T0 + 5000, "src": "kalshi_rest", "type": "orderbook", "ticker": DAL,
            "book": {"yes_dollars": [["0.7000", "5.00"]], "no_dollars": [["0.2500", "5.00"]]}}
    s.apply_rest(rest)
    assert s.markets[DAL].bid == 0.79  # WebSocket still authoritative
    s.apply_rest({**rest, "recv_ts_ms": T0 + 60_000})
    assert (s.markets[DAL].bid, s.markets[DAL].ask, s.markets[DAL].source) == (0.70, 0.75, "rest")


def test_fair_values_match_identical_pinnacle_lines():
    s = BoardState()
    s.apply_ws(markets_record())
    s.apply_odds(odds_record(T0 + 1000))
    assert s.markets[DAL].fair == devig(-444, 352)
    assert s.markets[TB].fair == devig(352, -444)
    assert s.markets[DAL_SPREAD].fair == devig(-102, -110)
    assert s.markets[TOTAL].fair == devig(-109, -105)
    g = s.games["26OCT08TBDAL"]
    assert g.kickoff_ms == iso_ms("2026-10-09T00:15:00Z") and not g.kickoff_estimated and g.odds_event_id == "e1"
    assert s.health["odds"].credits == "470"


def test_whole_number_pinnacle_spread_has_no_fair_value():
    s = BoardState()
    s.apply_ws(markets_record())
    s.apply_odds(odds_record(T0, spread_point=-7.5))
    assert s.markets[DAL_SPREAD].fair is not None
    s.apply_odds(odds_record(T0 + 1000, spread_point=-8.0))  # line moved to a whole number: push risk, no match
    assert s.markets[DAL_SPREAD].fair is None


def test_market_meta_inferred_from_ticker_when_no_market_list_seen():
    s = BoardState()
    s.apply_ws(snapshot(T0, ticker="KXNFLSPREAD-26OCT11SFSEA-SEA4"))
    meta = s.metas["KXNFLSPREAD-26OCT11SFSEA-SEA4"]
    assert (meta.kind, meta.team, meta.strike, meta.inferred) == ("spread", "SEA", 3.5, True)


def test_resume_does_not_reinsert_history_already_stored():
    s = BoardState(history_after_ms=T0 + 1500)
    s.apply_ws(snapshot(T0 + 1000))
    s.apply_ws(snapshot(T0 + 2000, yes=(("0.7800", "1.00"),)))
    assert [r[1] for r in s.drain()["tob"]] == [T0 + 2000]


# --- storage + API -----------------------------------------------------------------

@pytest.fixture
def board_db(tmp_path):
    s = BoardState()
    s.apply_ws(markets_record())
    s.apply_ws(snapshot(T0 + 1000))
    s.apply_ws(ws(T0 + 3000, "trade", {"market_ticker": DAL, "yes_price_dollars": "0.8100", "count_fp": "12.00", "taker_side": "yes", "ts_ms": T0 + 3000}, sid=2))
    s.apply_odds(odds_record(T0 + 4000))
    path = tmp_path / "board.db"
    conn = db.connect(path)
    db.init(conn)
    writer = db.Writer(conn)
    rev = writer.flush(s, s.drain())
    s.apply_ws(snapshot(T0 + 5000, yes=(("0.8000", "7.00"),)))
    writer.flush(s, s.drain())
    conn.close()
    return path, rev


def test_snapshot_updates_history_depth(board_db):
    path, first_rev = board_db
    b = Board(path)
    snap = b.snapshot()
    assert snap["rev"] == first_rev + 1 and len(snap["markets"]) == 4 and snap["games"][0]["label"] == "TB @ DAL"
    dal = next(m for m in snap["markets"] if m["ticker"] == DAL)
    assert (dal["bid"], dal["fair"], dal["trades_today"]) == (0.80, devig(-444, 352), 1)
    upd = b.updates(first_rev, 0)
    assert [m["ticker"] for m in upd["markets"]] == [DAL] and upd["tape"]
    assert b.updates(first_rev + 99, 0)["resync"] is True
    hist = b.history(DAL, "all")
    assert [p[1] for p in hist["tob"]] == [0.79, 0.80] and len(hist["trades"]) == 1 and hist["fair"]
    assert b.depth(DAL)["bids"] == [[0.8, 7.0]]


def test_downsample_keeps_shape_and_bounds():
    pts = [[i, i % 7] for i in range(10_000)]
    out = downsample(pts, 100)
    assert len(out) <= 102 and out[0] == pts[0] and out[-1] == pts[-1]


def test_http_api_and_static_files(board_db, tmp_path):
    path, _ = board_db
    Handler.board = Board(path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        snap = json.load(urllib.request.urlopen(base + "/api/snapshot"))
        assert len(snap["markets"]) == 4
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "Fairline Board" in html
        for bad in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/nope.js"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + bad)
            assert err.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/history")  # missing ticker
        assert err.value.code == 400
        Handler.board = Board(tmp_path / "missing.db")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/snapshot")
        assert err.value.code == 503 and json.load(err.value)["error"] == "no_database"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_indexer_replays_closed_hours_into_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FAIRLINE_DATA_DIR", str(tmp_path))
    from board.indexer import Indexer

    def write(stream, hh, records):
        p = _hour(tmp_path, stream, "2026-10-07", hh)
        p.write_text("".join(json.dumps(r) + "\n" for r in records))

    write("kalshi_ws", "17", [markets_record(T0), snapshot(T0 + 1000)])
    write("odds", "17", [odds_record(T0 + 500)])
    write("kalshi_ws", "18", [snapshot(T0 + 3_600_000, yes=(("0.8000", "3.00"),))])
    idx = Indexer()
    idx.replay()
    conn = db.connect(tmp_path / "board.db", readonly=True)
    rows = {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM market_state")}
    assert rows[DAL]["bid"] == 0.80 and rows[DAL]["fair"] == devig(-444, 352)
    assert conn.execute("SELECT COUNT(*) FROM top_of_book WHERE ticker = ?", (DAL,)).fetchone()[0] == 2
    assert int(db.get_meta(conn, "last_ts_ms")) == T0 + 3_600_000
    conn.close()
    idx.conn.close()


def test_resume_boundary_is_decided_by_receive_time():
    s = BoardState(history_after_ms=T0 + 1500)
    s.apply_ws(snapshot(T0 + 1000))  # already stored before the restart
    # Arrives after the checkpoint even though Kalshi stamped the change slightly earlier: must be kept.
    s.apply_ws(ws(T0 + 1600, "orderbook_delta", {"market_ticker": DAL, "price_dollars": "0.8000", "delta_fp": "4.00", "side": "yes", "ts_ms": T0 + 1400}, seq=2))
    assert [r[1] for r in s.drain()["tob"]] == [T0 + 1400]
