"""Paper book: the minute replay (take and quote rules, no look-ahead, blind minutes), settlement,
scoring and the end-to-end run over a board database and an odds tape."""
import gzip
import json

import pytest

from board import db
from board.paper import (
    FAIR_FRESH_MS, PaperBook, PinnacleTape, Settlements, fair_series, replay, score, summarize, tally, variant_id,
)
from board.state import BoardState
from fairvalue.devig import power
from fairvalue.fees import order_fee
from fairvalue.odds import american_to_prob

MIN = 60_000
T0 = 1791400000000 // MIN * MIN  # 2026-10-07 ~15:06 ET, on a minute boundary
KICK = T0 + 120 * MIN
M = {"ticker": "KXNFLGAME-26OCT08TBDAL-DAL", "game_key": "26OCT08TBDAL", "kind": "winner", "label": "DAL", "kickoff_ms": KICK, "trade": True}


def every_20_min(fair):
    """A Pinnacle price arriving every 20 minutes through kickoff (older than 35 minutes is not trusted)."""
    return [(T0 + i * 20 * MIN, fair) for i in range(7)]


def every_2_min(fair):
    """A Pinnacle price arriving every 2 minutes, as in the 3 hours before kickoff."""
    return [(T0 + i * 2 * MIN, fair) for i in range(60)]


def by_variant(res):
    return {b["variant"]: b for b in res["bets"]}


# --- the minute replay -----------------------------------------------------------------

def test_take_fires_at_the_first_minute_the_edge_clears_each_threshold():
    # Fair 60c, ask 56c: edge = 4c - taker fee on 100 at 56c (1.73c) = 2.27c.
    tob = [(T0, 0.55, 0.56, 500, 500)]
    res = replay(M, tob, [], [(T0, 0.60)], [], now=KICK + MIN)
    bets = by_variant(res)
    fee = order_fee(0.56, 100, "taker")
    assert fee == 1.73
    for threshold in (0, 0.5, 1, 2):
        b = bets[f"take@{threshold:g}"]
        assert (b["side"], b["price"], b["qty"], b["fee"], b["ts_ms"]) == ("yes", 0.56, 100, fee, T0 + MIN)
    assert "take@3" not in bets  # 2.27c does not clear 3c


def test_take_buys_no_when_kalshi_is_too_high_and_sizes_to_the_book():
    tob = [(T0, 0.66, 0.67, 40, 500)]  # NO costs 1 - 0.66 = 34c, fair NO = 40c, only 40 contracts bid
    res = replay(M, tob, [], [(T0, 0.60)], [], now=KICK + MIN)
    b = by_variant(res)["take@2"]
    assert (b["side"], b["price"], b["qty"]) == ("no", 0.34, 40)
    assert b["fair"] == pytest.approx(0.40)


def test_no_look_ahead_a_price_counts_only_once_received():
    tob = [(T0, 0.55, 0.56, 500, 500)]
    fairs = [(T0, 0.555), (T0 + 30 * MIN + 5_000, 0.60)]  # fair sits at the mid, then a move arrives 30 min in
    b = by_variant(replay(M, tob, [], fairs, [], now=KICK + MIN))["take@2"]
    assert b["ts_ms"] == T0 + 31 * MIN


def test_stale_fair_and_blind_minutes_are_skipped():
    tob = [(T0, 0.55, 0.56, 500, 500)]
    # The only Pinnacle price arrived long before the window: too old to trust.
    stale = replay(M, tob, [], [(T0 - FAIR_FRESH_MS - 2 * MIN, 0.60), (T0 - FAIR_FRESH_MS - MIN, 0.60)], [], now=KICK + MIN)
    assert stale["bets"] == [] and stale["gap"]["minutes"] == 0
    # The recorder was blind for the first 40 minutes: no bet until it could see again.
    b = by_variant(replay(M, tob, [], every_2_min(0.60), [(T0, T0 + 40 * MIN)], now=KICK + MIN))["take@2"]
    assert b["ts_ms"] == T0 + 40 * MIN


def test_rules_only_act_on_a_recent_pinnacle_price():
    # The edge appears 10 minutes after the only Pinnacle price: still measured, never traded.
    tob = [(T0, 0.59, 0.60, 500, 500), (T0 + 10 * MIN, 0.55, 0.56, 500, 500)]
    res = replay(M, tob, [], [(T0, 0.60)], [], now=KICK + MIN)
    assert res["bets"] == [] and res["gap"]["qualifying"] > 0


def test_crossed_books_are_skipped():
    tob = [(T0, 0.59, 0.51, 500, 500), (T0 + 5 * MIN, 0.50, 0.51, 500, 500)]  # bid above ask: a missed update
    res = replay(M, tob, [], every_2_min(0.51), [], now=T0 + 10 * MIN)
    assert res["bets"] == [] and res["gap"]["minutes"] == 5


def test_quote_fills_only_on_a_trade_through_the_quote():
    tob = [(T0, 0.49, 0.51, 500, 500)]
    fairs = every_2_min(0.50)  # quote +-2c: bid 48c, offer 52c
    trades = [
        (T0 + 2 * MIN + 1, 0.48, 50, "no"),    # sold at our bid: no proof we were filled
        (T0 + 5 * MIN + 1, 0.47, 30, "no"),    # sold through it: our bid fills, 30 contracts
        (T0 + 6 * MIN + 1, 0.52, 80, "yes"),   # bought at our offer: no fill
        (T0 + 9 * MIN + 1, 0.53, 200, "yes"),  # bought through it: our offer fills, capped at 100
    ]
    q = [b for b in replay(M, tob, trades, fairs, [], now=KICK + MIN)["bets"] if b["variant"] == "quote@2"]
    yes = next(b for b in q if b["side"] == "yes")
    no = next(b for b in q if b["side"] == "no")
    assert (yes["price"], yes["qty"], yes["ts_ms"], yes["fee"]) == (0.48, 30, T0 + 5 * MIN, order_fee(0.48, 30, "maker", "quadratic_with_maker_fees"))
    assert (no["price"], no["qty"], no["ts_ms"]) == (0.48, 100, T0 + 9 * MIN)
    assert no["fair"] == pytest.approx(0.50)


def test_quotes_never_cross_the_book():
    tob = [(T0, 0.40, 0.42, 500, 500)]
    # Fair 60c would put the bid at 58c, above the 42c offer; post-only caps it at 41c.
    trades = [(T0 + 2 * MIN, 0.40, 10, "no")]
    q = [b for b in replay(M, tob, trades, [(T0, 0.60)], [], now=KICK + MIN)["bets"] if b["variant"] == "quote@2"]
    assert q[0]["price"] == 0.41 and q[0]["side"] == "yes"


def test_away_winner_market_is_measured_but_not_traded():
    tob = [(T0, 0.55, 0.56, 500, 500)]
    res = replay({**M, "trade": False}, tob, [], every_20_min(0.60), [], now=KICK + MIN)
    assert res["bets"] == [] and res["gap"]["minutes"] == 119


def test_gap_measurement_counts_qualifying_side_minutes():
    # Mid 50c, spread 2c: fair 53c is 3c from the mid, 2c beyond half the spread -> qualifies.
    tob = [(T0, 0.49, 0.51, 500, 500)]
    g = replay(M, tob, [], [(T0, 0.53)], [], now=T0 + 11 * MIN)["gap"]
    assert g["minutes"] == 10 and g["qualifying"] == 10
    assert g["abs_sum"] == pytest.approx(0.30)
    # Stops at kickoff even when now is later; a single price goes stale after 35 minutes.
    assert replay(M, tob, [], every_20_min(0.53), [], now=KICK + 600 * MIN)["gap"]["minutes"] == 119
    assert replay(M, tob, [], [(T0, 0.53)], [], now=KICK + 600 * MIN)["gap"]["minutes"] == FAIR_FRESH_MS // MIN


def test_closing_values_are_the_last_before_kickoff():
    tob = [(T0, 0.49, 0.51, 1, 1), (KICK - MIN, 0.55, 0.57, 1, 1), (KICK + MIN, 0.90, 0.92, 1, 1)]
    res = replay(M, tob, [], [(T0, 0.5), (KICK - 2 * MIN, 0.56), (KICK + MIN, 0.95)], [], now=KICK + 10 * MIN)
    assert res["close_fair"] == 0.56 and res["close_mid"] == 0.56


# --- settlement and scoring --------------------------------------------------------------

BET = {"variant": "take@2", "rule": "take", "ticker": M["ticker"], "game_key": M["game_key"], "kind": "winner", "label": "DAL",
       "side": "yes", "ts_ms": T0, "kickoff_ms": KICK, "price": 0.56, "qty": 100, "fee": 1.73, "fair": 0.60}


def test_score_won_lost_tie_void_and_pending():
    won = score(BET, 1.0, "yes", KICK + 4 * 3600_000, 0.62, now=KICK + 5 * 3600_000)
    assert won["status"] == "won" and won["pnl"] == pytest.approx(100 * 0.44 - 1.73)
    assert won["clv"] == pytest.approx(0.62 - 0.56 - 0.0173) and won["edge"] == pytest.approx(0.60 - 0.56 - 0.0173)
    assert won["cost"] == pytest.approx(57.73) and won["ev"] == pytest.approx(2.27)
    lost = score({**BET, "side": "no", "price": 0.44, "fair": 0.40}, 1.0, "yes", KICK, 0.62, now=KICK + 1)
    assert lost["status"] == "lost" and lost["pnl"] == pytest.approx(-44 - 1.73) and lost["close"] == pytest.approx(0.38)
    tie = score(BET, 0.5, "scalar", KICK, None, now=KICK + 1)
    assert tie["status"] == "tie" and tie["pnl"] == pytest.approx(100 * (0.5 - 0.56) - 1.73) and tie["clv"] is None
    assert score(BET, None, "void", KICK, None, now=KICK + 1)["pnl"] == 0.0
    assert score(BET, None, None, None, 0.62, now=KICK - 1)["status"] == "open"
    live = score(BET, None, None, None, 0.62, now=KICK + 1)
    assert live["status"] == "live" and live["clv"] is not None


def test_tally_and_summary():
    bets = [score({**BET, "ts_ms": T0 + i}, 1.0 if i % 3 else 0.0, "yes" if i % 3 else "no", KICK + i, 0.6, now=KICK + 99) for i in range(6)]
    t = tally(bets)
    assert (t["won"], t["lost"], t["tied"], t["settled"]) == (4, 2, 0, 6)
    assert t["win_pct"] == pytest.approx(4 / 6)
    assert t["pnl"] == pytest.approx(4 * 42.27 - 2 * 57.73)
    s = summarize(bets)
    assert s["form"] == ["L", "W", "W", "L", "W", "W"]
    assert s["curve"][-1][1] == pytest.approx(t["pnl"])
    assert [r["key"] for r in s["splits"]["kind"]["rows"]] == ["winner"]


def test_settlements_fetch_once_final_and_recheck_open(tmp_path):
    calls = []
    answers = {"A": {"ticker": "A", "status": "finalized", "result": "yes", "settlement_ts": "2026-10-11T20:11:38Z", "settlement_value_dollars": "1.0000"},
               "B": {"ticker": "B", "status": "active", "result": ""}}

    def fetch(tickers):
        calls.append(list(tickers))
        return [answers[t] for t in tickers]

    s = Settlements(tmp_path / "s.json", fetch)
    s.update(["A", "B"], now=T0)
    assert calls == [["A", "B"]]
    assert s.result("A")[:2] == (1.0, "yes") and s.result("B") == (None, None, None)
    s.update(["A", "B"], now=T0 + MIN)  # too soon to re-check B; A is final
    assert len(calls) == 1
    s.update(["A", "B"], now=T0 + 11 * MIN)
    assert calls[-1] == ["B"]
    again = Settlements(tmp_path / "s.json", fetch)  # cached on disk
    assert again.result("A")[:2] == (1.0, "yes")


def test_settlement_outage_keeps_what_we_have(tmp_path):
    def down(tickers):
        raise OSError("offline")

    s = Settlements(tmp_path / "s.json", down)
    s.update(["A"], now=T0)
    assert s.error and "offline" in s.error and s.result("A") == (None, None, None)


# --- end to end ------------------------------------------------------------------------

def _db(path):
    conn = db.connect(path)
    db.init(conn)
    with conn:
        conn.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)", ("26OCT08TBDAL", "nfl", "2026-10-08", "TB", "DAL", "TB @ DAL", KICK, 0, "e1"))
        for team in ("DAL", "TB"):
            conn.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (f"KXNFLGAME-26OCT08TBDAL-{team}", "nfl", "KXNFLGAME", "KXNFLGAME-26OCT08TBDAL", "26OCT08TBDAL", "winner", team, None, team, team, 0))
        conn.execute("INSERT INTO top_of_book VALUES (?,?,?,?,?,?)", ("KXNFLGAME-26OCT08TBDAL-DAL", T0, 0.50, 0.52, 300, 300))
        conn.execute("INSERT INTO top_of_book VALUES (?,?,?,?,?,?)", ("KXNFLGAME-26OCT08TBDAL-TB", T0, 0.48, 0.50, 300, 300))
    conn.close()


def _odds(data, ts, last_update=None):
    h2h = {"key": "h2h", "outcomes": [{"name": "Dallas Cowboys", "price": -150}, {"name": "Tampa Bay Buccaneers", "price": 130}]}
    if last_update:
        h2h["last_update"] = last_update
    rec = {"recv_ts_ms": ts, "src": "odds_api", "league": "nfl", "usage": {"x-requests-last": "3"}, "events": [{
        "id": "e1", "commence_time": "2026-10-07T21:06:40Z", "home_team": "Dallas Cowboys", "away_team": "Tampa Bay Buccaneers",
        "bookmakers": [{"key": "pinnacle", "markets": [h2h]}]}]}
    path = data / "raw" / "odds" / "2026-10-07" / "19.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress((json.dumps(rec, separators=(",", ":")) + "\n").encode()))  # compact, as the recorder writes


def test_paper_book_end_to_end(tmp_path):
    _db(tmp_path / "board.db")
    _odds(tmp_path, T0 + 1000)
    fetched = []

    def fetch(tickers):
        fetched.extend(tickers)
        return [{"ticker": t, "status": "finalized", "result": "yes", "settlement_ts": "2026-10-08T03:30:00Z"} for t in tickers]

    book = PaperBook(tmp_path, tmp_path / "board.db", Settlements(tmp_path / "s.json", fetch))
    res = book.run([], KICK + 20 * 60_000, now=KICK + 20 * 60_000)
    fair = round(power([american_to_prob(-150), american_to_prob(130)])[0], 5)
    take = {v["id"]: v for v in res["variants"]}["take@2"]
    assert fair - 0.52 - 0.0175 > 0.02 and take["bets"] == 1 and take["won"] == 1
    bet = next(b for b in res["bets"] if b["variant"] == "take@2")
    assert bet["ticker"].endswith("-DAL") and bet["fair"] == fair  # home team's market only
    assert fetched == ["KXNFLGAME-26OCT08TBDAL-DAL"]
    assert res["g2"]["available"] and res["g2"]["markets"] == 2
    assert res["games"][0]["label"] == "TB @ DAL" and res["games"][0]["close"]["mid"] == 0.51
    # The finished game's replay is kept; a second run reads nothing new from the database.
    assert "KXNFLGAME-26OCT08TBDAL-DAL" in book.final
    assert [v["id"] for v in res["variants"]] == [variant_id(r, p) for r, p in
                                                   [("take", 0), ("take", .005), ("take", .01), ("take", .02), ("take", .03),
                                                    ("quote", .005), ("quote", .01), ("quote", .02), ("quote", .03)]]


def test_pinnacle_line_not_refreshed_counts_as_pulled(tmp_path):
    _db(tmp_path / "board.db")
    conn = db.connect(tmp_path / "board.db", readonly=True)
    state = BoardState()
    db.load_catalog(conn, state)
    conn.close()
    _odds(tmp_path, T0 + 1000, last_update="2026-10-07T18:50:00Z")  # 16 minutes before we received it
    series = fair_series(PinnacleTape(tmp_path).polls(), state)
    assert series["KXNFLGAME-26OCT08TBDAL-DAL"] == [(T0 + 1000, None)]


def test_fair_series_uses_receive_time(tmp_path):
    _db(tmp_path / "board.db")
    conn = db.connect(tmp_path / "board.db", readonly=True)
    state = BoardState()
    db.load_catalog(conn, state)
    conn.close()
    _odds(tmp_path, T0 + 1234)
    from board.paper import PinnacleTape
    series = fair_series(PinnacleTape(tmp_path).polls(), state)
    assert series["KXNFLGAME-26OCT08TBDAL-DAL"][0][0] == T0 + 1234
    assert series["KXNFLGAME-26OCT08TBDAL-TB"][0][1] == pytest.approx(1 - series["KXNFLGAME-26OCT08TBDAL-DAL"][0][1], abs=1e-4)


def test_clv_summary_numbers_g4_reads():
    """G4 decides on the mean CLV and its 95% interval; pin both, and the share that beat the close."""
    from board.paper import tally
    bets = [{"status": "won", "cost": 50.0, "pnl": 10.0, "ev": 1.0, "edge": 0.01, "clv": c} for c in (0.01, 0.03, -0.01, 0.05)]
    out = tally(bets)
    assert out["clv_n"] == 4 and out["clv_mean"] == pytest.approx(0.02)
    sd = (sum((c - 0.02) ** 2 for c in (0.01, 0.03, -0.01, 0.05)) / 3) ** 0.5  # sample SD, n - 1
    assert out["clv_ci"] == pytest.approx(1.96 * sd / 2)
    assert out["beat_close"] == 0.75


def test_gap_by_time_to_kickoff_buckets_the_same_minutes():
    """by_lead splits the G2 minutes into 5-minute buckets counted back from kickoff."""
    from board.paper import g2_summary
    gap = {"minutes": 3, "qualifying": 1, "abs_sum": 0.06, "days": {"2026-10-07": [3, 1]},
           "abs_hist": {10: 2, 40: 1}, "beyond_hist": {20: 3},
           "lead": {0: [2, 1, 0.05, {10: 1, 40: 1}], 35: [1, 0, 0.01, {10: 1}]}}
    out = g2_summary([{"gap": gap, "kind": "spread"}], now=1791400000000)
    rows = out["by_lead"]["rows"]
    assert [r["lead_min"] for r in rows] == [175, 0]  # oldest first
    assert rows[1]["minutes"] == 2 and rows[1]["qualifying"] == 1
    assert rows[1]["median"] == pytest.approx(0.0105) and rows[0]["mean"] == pytest.approx(0.01)
    assert sum(r["minutes"] for r in rows) == out["side_minutes"]
