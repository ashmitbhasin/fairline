"""Tracker: tape scanning, recorder blind spots and uptime, the background worker and the HTTP API."""
import datetime as dt
import gzip
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from board.server import Board, Handler
from board.tracker import (
    TapeIndex, Tracker, blind_spots, last_minute, log_heartbeats, mapping_coverage, odds_burn, per_day,
    recent_blind_spots, scan_file, uptime, uptime_by_day,
)

MIN = 60_000
T0 = 1791400000000  # 2026-10-07 ~15:06 ET


def _write(path, records, gz=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()
    path.write_bytes(gzip.compress(body) if gz else body)


# --- tape scanning --------------------------------------------------------------------

def test_scan_file_counts_records_minutes_and_recorder_events(tmp_path):
    f = tmp_path / "x.jsonl.gz"
    _write(f, [
        {"recv_ts_ms": T0, "src": "recorder", "type": "_connected"},
        {"recv_ts_ms": T0 + 5_000, "src": "kalshi_ws", "m": {"type": "orderbook_delta"}},
        {"recv_ts_ms": T0 + 3 * MIN, "src": "recorder", "type": "_disconnected", "error": "x"},
    ], gz=True)
    res = scan_file(f, want_events=True, want_usage=False)
    assert res["records"] == 3 and len(res["minutes"]) == 2
    assert [e[1] for e in res["events"]] == ["_connected", "_disconnected"]


def test_tape_index_caches_closed_files_and_rolls_up_days(tmp_path):
    raw = tmp_path / "raw" / "kalshi_ws" / "2026-10-07"
    _write(raw / "19.jsonl.gz", [{"recv_ts_ms": T0, "src": "kalshi_ws", "m": {}}], gz=True)
    idx = TapeIndex(tmp_path, tmp_path / "cache.json")
    first = idx.refresh()
    assert first["kalshi_ws"][0]["records"] == 1
    again = TapeIndex(tmp_path, tmp_path / "cache.json")  # reloads the cache from disk
    assert again.files and again.refresh()["kalshi_ws"][0]["records"] == 1
    days = per_day(first, 2, T0 + 3600_000)
    assert days[-1]["day"] == "2026-10-07" and days[-1]["kalshi_ws"]["records"] == 1


def test_odds_burn_and_mapping():
    files = [{"usage": [[T0 - 3 * 86_400_000, 3], [T0 - 86_400_000, 3], [T0, 3]]}]
    b = odds_burn(files, T0)
    assert b["credits_7d"] == 9 and b["projected_30d"] == 90
    games = [{"kickoff_ms": T0, "odds_event_id": "x"}, {"kickoff_ms": T0, "odds_event_id": None}]
    assert mapping_coverage(games, T0)["share"] == 0.5


# --- blind spots and uptime -------------------------------------------------------------

def test_blind_spots_from_disconnects_and_silence():
    beats = [T0 + i * MIN for i in range(60) if not (20 <= i < 50)]  # a 30-minute freeze from minute 20
    files = [{"events": [[T0 + 10 * MIN, "_disconnected"], [T0 + 10 * MIN + 2_000, "_connected"]], "minutes": []}]
    down = blind_spots(files, beats, now=T0 + 70 * MIN)
    assert down[0] == (T0 + 10 * MIN, T0 + 10 * MIN + 2_000)
    assert down[1] == (T0 + 20 * MIN, T0 + 50 * MIN)  # one beat interval after the last sign of life
    assert down[2] == (T0 + 60 * MIN, T0 + 70 * MIN)  # silent since minute 59 until now


def test_uptime_counts_disconnects_and_freezes_only_where_measured():
    beats = [T0 + i * MIN for i in range(60) if not (20 <= i < 50)]
    files = [{"events": [[T0 + 10 * MIN, "_disconnected"], [T0 + 10 * MIN + 2_000, "_connected"]], "minutes": []}]
    now = T0 + 60 * MIN
    down = blind_spots(files, beats, now)
    u = uptime(down, beats[0] - MIN, [], now)
    assert 29.9 < u["down_ms"] / MIN < 30.2 and u["outages"] == 2
    assert 0.48 < u["uptime"] < 0.52
    assert uptime(down, None, [], now)["available"] is False


def test_recent_blind_spots_from_log_heartbeats_alone():
    beats = [T0 + i * MIN for i in range(10)] + [T0 + 30 * MIN]
    assert recent_blind_spots(beats, T0 + 5 * MIN, T0 + 31 * MIN) == [(T0 + 10 * MIN, T0 + 30 * MIN)]
    assert recent_blind_spots([], T0, T0 + 10 * MIN) == [(T0, T0 + 10 * MIN)]  # nothing since the scan: blind
    assert recent_blind_spots([], T0, T0 + MIN) == []


def test_uptime_by_day_is_unmeasured_before_heartbeats():
    day_start = int(dt.datetime(2026, 10, 7, tzinfo=dt.timezone(dt.timedelta(hours=-4))).timestamp() * 1000)
    now = day_start + 30 * 3600_000  # 6 AM on Oct 8
    days = uptime_by_day([(day_start + 2 * 3600_000, day_start + 8 * 3600_000)], day_start, now, days=3)
    assert days["2026-10-06"] is None
    assert days["2026-10-07"] == pytest.approx(0.75)
    assert days["2026-10-08"] == pytest.approx(1.0)


def test_log_heartbeats_and_last_minute(tmp_path):
    log = tmp_path / "kalshi-live.log"
    log.write_text("2026-10-12 18:46:30,364 INFO recorder_ws: last 60s: {'msgs': 1, 'deltas': 1, 'trades': 0, 'gaps': 0, 'errors': 0}\n"
                   "2026-10-12 18:46:31,000 WARNING recorder_ws: connection lost\n")
    beats = log_heartbeats(log)
    assert len(beats) == 1 and beats[0] == int(dt.datetime(2026, 10, 12, 18, 46, 30).timestamp() * 1000) + 364
    assert last_minute(log) == {"ts_ms": beats[0], "msgs": 1, "deltas": 1, "trades": 0, "gaps": 0, "errors": 0}
    assert last_minute(tmp_path / "missing.log") is None


def test_tracker_refresh_on_empty_data(tmp_path):
    tr = Tracker(tmp_path, tmp_path / "board.db", fetch=lambda t: [])
    res = tr.refresh()
    assert res["tape"]["uptime"]["available"] is False and res["paper"]["available"] is False
    snap = tr.snapshot()
    assert snap["refreshing"] is False and "services" in snap["now"] and snap["now"]["edges"]["rows"] == []
    assert (tmp_path / "tracker" / "stats.json").exists()


# --- HTTP ------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    Handler.board = Board(tmp_path / "missing.db")
    Handler.tracker = Tracker(tmp_path, tmp_path / "missing.db", fetch=lambda t: [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    Handler.tracker = None


def _post(url, headers=None):
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(req)


def test_tracker_api_reads_and_recomputes(server):
    data = json.load(urllib.request.urlopen(server + "/api/tracker"))
    assert {"now", "now_ms", "refreshing"} <= data.keys()
    out = json.load(_post(server + "/api/tracker/refresh", {"X-Fairline": "1", "Origin": server}))
    assert out["started"] is True


@pytest.mark.parametrize("headers", [{}, {"X-Fairline": "1", "Origin": "https://evil.example"}])
def test_cross_site_requests_are_refused(server, headers):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server + "/api/tracker/refresh", headers)
    assert err.value.code == 403


def test_nothing_else_accepts_writes(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server + "/api/tracker/tasks", {"X-Fairline": "1"})
    assert err.value.code == 404


def test_tracker_page_assets_are_served(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert 'id="tracker"' in html and "/tracker.js" in html
    js = urllib.request.urlopen(server + "/tracker.js")
    assert js.headers["Content-Type"].startswith("text/javascript")


# ---------- Now: recorders on EC2 ----------

def _remote(tmp_path, monkeypatch, exists=True):
    from tape import ctl
    cfg = tmp_path / "ssh_config"
    if exists:
        cfg.write_text("Host fairline\n")
    monkeypatch.setattr(ctl, "REMOTE_RECORDER", cfg)
    (tmp_path / "run").mkdir(exist_ok=True)


def test_now_without_a_server_shows_local_recorders_and_no_pull(tmp_path, monkeypatch):
    from board.tracker import now_services
    _remote(tmp_path, monkeypatch, exists=False)
    out = now_services(tmp_path, T0)
    assert [s["name"] for s in out["services"]] == ["kalshi-live", "kalshi-backup", "odds", "board", "board-web"]
    assert out["remote"] is None and out["disk"]["where"] == "this Mac"


def test_now_shows_the_ec2_report_while_it_is_fresh(tmp_path, monkeypatch):
    from board.tracker import EC2_NOW, LAST_PULL, now_services
    _remote(tmp_path, monkeypatch)
    report = {"ts_ms": T0 - MIN, "services": [{"name": "kalshi-live", "stream": "kalshi_ws", "running": True, "started_ms": T0 - 60 * MIN,
                                                "last_data_ms": T0 - 1000, "url": None}],
              "recorder": {"ts_ms": T0 - 30_000, "msgs": 2273}, "backup": {"ts_ms": T0, "markets": 650, "changed": 30},
              "odds": {"remaining": 18539}, "disk": {"free": 14, "total": 19}}
    (tmp_path / "run" / EC2_NOW).write_text(json.dumps(report))
    (tmp_path / "run" / LAST_PULL).write_text(json.dumps({"ts_ms": T0, "ok": True}))
    out = now_services(tmp_path, T0)
    rows = {s["name"]: s for s in out["services"]}
    assert list(rows) == ["kalshi-live", "kalshi-backup", "odds", "board", "board-web", "pull"]
    assert rows["kalshi-live"]["running"] is True and rows["kalshi-live"]["where"] == "EC2"
    assert rows["kalshi-backup"]["running"] is False  # missing from the report: not running there
    assert rows["pull"]["last_pull"]["ok"] is True
    assert out["recorder"]["msgs"] == 2273 and out["odds"]["remaining"] == 18539
    assert out["disk"] == {"free": 14, "total": 19, "where": "EC2"} and out["remote"]["fresh"] is True


def test_now_leaves_ec2_state_unknown_when_the_report_is_old(tmp_path, monkeypatch):
    from board.tracker import EC2_NOW, now_services
    _remote(tmp_path, monkeypatch)
    (tmp_path / "run" / EC2_NOW).write_text(json.dumps({"ts_ms": T0 - 10 * MIN, "services": [{"name": "kalshi-live", "running": True}]}))
    out = now_services(tmp_path, T0)
    assert [s["running"] for s in out["services"][:3]] == [None, None, None]
    assert out["remote"] == {"reported_ms": T0 - 10 * MIN, "fresh": False}
    (tmp_path / "run" / EC2_NOW).unlink()
    assert now_services(tmp_path, T0)["services"][0]["running"] is None


def test_last_cycle_reads_the_backup_recorders_latest_poll(tmp_path):
    from board.tracker import last_cycle
    log = tmp_path / "kalshi-backup.log"
    log.write_text("2026-10-26 18:12:06,879 INFO recorder_rest: tracking 650 markets\n"
                   "2026-10-26 18:13:02,268 INFO recorder_rest: cycle: 650 markets, 30 changed books, fast mode\n")
    out = last_cycle(log)
    assert out["markets"] == 650 and out["changed"] == 30
    assert last_cycle(tmp_path / "missing.log") is None


def test_report_lists_only_the_recorders(tmp_path):
    from board.tracker import recorder_report
    out = recorder_report(tmp_path)
    assert [s["name"] for s in out["services"]] == ["kalshi-live", "kalshi-backup", "odds"]
    assert out["disk"]["total"] > 0


def test_ec2_minutes_not_yet_copied_count_neither_way(tmp_path, monkeypatch):
    from board.tracker import LAST_PULL, known_until
    _remote(tmp_path, monkeypatch, exists=False)
    assert known_until(tmp_path, T0) == T0  # recorders on this machine: judged up to now
    _remote(tmp_path, monkeypatch)
    assert known_until(tmp_path, T0) == T0  # never copied yet: nothing to stop at
    (tmp_path / "run" / LAST_PULL).write_text(json.dumps({"ts_ms": T0 - MIN, "ok": False, "ok_ms": T0 - 8 * MIN}))
    known = known_until(tmp_path, T0)
    assert known == T0 - 8 * MIN  # the last copy that worked
    beats = [T0 - 60 * MIN + i * MIN for i in range(52)]  # heartbeats up to the last copy
    assert blind_spots([], beats, known) == []
    assert blind_spots([], beats, T0) != []  # judged up to now, the uncopied minutes looked blind


def test_pull_records_the_last_good_copy_and_asks_for_a_rescan(tmp_path, monkeypatch):
    from tape import pull
    calls = []
    monkeypatch.setattr(pull, "recompute", lambda: calls.append("rescan"))
    ok = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    bad = type("P", (), {"returncode": 255, "stdout": "", "stderr": "ssh: Network is unreachable"})()
    monkeypatch.setattr(pull.subprocess, "run", lambda *a, **k: ok)
    assert pull.pull(tmp_path) is True
    first = json.loads((tmp_path / "last-pull.json").read_text())
    assert first["ok_ms"] == first["ts_ms"] and calls == ["rescan"]
    monkeypatch.setattr(pull.subprocess, "run", lambda *a, **k: bad)
    assert pull.pull(tmp_path) is False
    second = json.loads((tmp_path / "last-pull.json").read_text())
    assert second["ok"] is False and second["ok_ms"] == first["ok_ms"] and calls == ["rescan"]


def test_a_rescan_asked_for_mid_recompute_is_queued_not_dropped(tmp_path):
    from board.tracker import Tracker
    tr = Tracker(tmp_path, tmp_path / "board.db", fetch=lambda tickers: {})
    tr.refreshing = True
    assert tr.request_refresh() is False
    assert tr.force_tape is True and tr.wake.is_set()


# --- blind time the markers do not show -------------------------------------------------

def test_a_reconnect_without_a_disconnect_still_counts_as_blind(tmp_path):
    """The live recorder ends each session every 30 minutes (and on a restart) without writing
    _disconnected; the seconds until the next _connected are blind and must count."""
    f = tmp_path / "17.jsonl"
    _write(f, [
        {"recv_ts_ms": T0, "src": "kalshi_ws", "m": {"type": "orderbook_delta"}},
        {"recv_ts_ms": T0 + 20_000, "src": "kalshi_ws", "m": {"type": "trade"}},
        {"recv_ts_ms": T0 + 25_000, "src": "recorder", "type": "_markets", "markets": []},
        {"recv_ts_ms": T0 + 31_000, "src": "recorder", "type": "_connected"},
        {"recv_ts_ms": T0 + 32_000, "src": "kalshi_ws", "m": {"type": "orderbook_snapshot"}},
    ])
    res = scan_file(f, want_events=True, want_usage=False)
    assert res["events"] == [[T0 + 31_000, "_connected", T0 + 20_000]] and res["last_seen"] == T0 + 32_000
    assert blind_spots([res], [], T0 + 60_000) == [(T0 + 20_000, T0 + 31_000)]


def test_a_reconnect_opening_an_hour_uses_the_last_hour_seen(tmp_path):
    a, b = tmp_path / "17.jsonl", tmp_path / "18.jsonl"
    _write(a, [{"recv_ts_ms": T0, "src": "kalshi_ws", "m": {"type": "trade"}}])
    _write(b, [{"recv_ts_ms": T0 + 9_000, "src": "recorder", "type": "_connected"},
               {"recv_ts_ms": T0 + 10_000, "src": "kalshi_ws", "m": {"type": "trade"}}])
    files = [scan_file(a, True, False), scan_file(b, True, False)]
    assert blind_spots(files, [], T0 + 30_000) == [(T0, T0 + 9_000)]


# --- the night the clocks go back (Sun Nov 1 2026, 2:00 EDT -> 1:00 EST) ----------------------

@pytest.fixture
def eastern(monkeypatch):
    import time as _time
    monkeypatch.setenv("TZ", "America/New_York")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


def test_log_heartbeats_through_the_repeated_hour(tmp_path, eastern):
    from board.tracker import local_log_ms
    start = 1793505600000  # 2026-11-01 04:00 UTC = 00:00 EDT
    lines = []
    for i in range(181):  # one a minute, 00:00 EDT to 02:00 EST
        t = dt.datetime.fromtimestamp((start + i * MIN) / 1000)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000 INFO recorder_ws: last 60s: {{'msgs': 5}}\n")
    log = tmp_path / "kalshi-live.log"
    log.write_text("".join(lines))
    beats = log_heartbeats(log)
    assert beats == [start + i * MIN for i in range(181)]
    assert recent_blind_spots(beats, start + 100 * MIN, start + 180 * MIN) == []
    # the newest line alone: the later 1:30 once it has happened, the earlier one before that
    assert local_log_ms("2026-11-01 01:30:00", "0", before_ms=start + 160 * MIN) == start + 150 * MIN
    assert local_log_ms("2026-11-01 01:30:00", "0", before_ms=start + 100 * MIN) == start + 90 * MIN


def test_uptime_by_day_covers_the_25_hour_day(eastern):
    from board.tracker import et_day_end, et_midnight
    nov1 = et_midnight("2026-11-01")
    assert et_day_end("2026-11-01") - nov1 == 25 * 3_600_000
    outage = [(et_day_end("2026-11-01") - 50 * MIN, et_day_end("2026-11-01") - 10 * MIN)]  # 23:10 to 23:50 EST
    days = uptime_by_day(outage, nov1 - 10 * 86_400_000, et_day_end("2026-11-02") - 1, days=3)
    assert list(days) == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert days["2026-11-01"] == pytest.approx(1 - 40 / (25 * 60))


def test_paper_waits_for_the_copied_tape_before_freezing_a_game(tmp_path, monkeypatch):
    from board import db
    from board.tracker import paper_known_until
    from tape import ctl
    now = T0 + 5 * 3_600_000
    assert paper_known_until(tmp_path, tmp_path / "board.db", now) == now  # recorders here: live
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host fairline\n")
    monkeypatch.setattr(ctl, "REMOTE_RECORDER", cfg)
    conn = db.connect(tmp_path / "board.db")
    db.init(conn)
    with conn:
        db.set_meta(conn, "complete_until_ms", T0 - T0 % 3_600_000)  # board complete to 19:00 UTC
    conn.close()
    hour = T0 - T0 % 3_600_000  # 2026-10-07 19:00 UTC
    _write(tmp_path / "raw" / "odds" / "2026-10-07" / "19.jsonl.gz", [{"recv_ts_ms": T0}], gz=True)  # odds to 20:00
    assert paper_known_until(tmp_path, tmp_path / "board.db", now) == hour  # the board is the earlier of the two


def test_server_refuses_a_rebound_hostname(tmp_path):
    """A page that points its own name at 127.0.0.1 must not read the board or trigger a recompute."""
    import http.client
    Handler.board = Board(tmp_path / "missing.db")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        for method, path, headers in (("GET", "/api/snapshot", {}), ("POST", "/api/tracker/refresh", {"X-Fairline": "1"})):
            c = http.client.HTTPConnection("127.0.0.1", port)
            c.request(method, path, headers={"Host": f"rebind.attacker.test:{port}", "Origin": f"http://rebind.attacker.test:{port}", **headers})
            assert c.getresponse().status == 403
            c.close()
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/snapshot", headers={"Host": f"localhost:{port}"})
        assert c.getresponse().status == 503  # allowed through; this test has no database
        c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
