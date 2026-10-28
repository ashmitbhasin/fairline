"""The tracker: Fairline's own output, computed from the tape and the board database. Nothing here is
entered by hand.

    paper    paper bets under the take and quote rule ladders, scored at Kalshi settlement, and the G2
             gap measurement from the same minute replay (board/paper.py). Every 5 minutes.
    tape     records, bytes and active minutes per hour file (closed hours are read once and cached in
             data/tracker/tapefiles.json): data per day, trades per day, recorder uptime and the blind
             spots the paper book skips. Hourly.
    now      services, stream health, the recorder's last minute, live edges, upcoming games and odds
             credits. On every request.

Results are kept in data/tracker/stats.json so a restarted board shows the last numbers at once.
"""
from __future__ import annotations

import ast
import datetime as dt
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import paper
from .paper import PaperBook, Settlements, fetch_markets

log = logging.getLogger("board.tracker")
ET = ZoneInfo("America/New_York")
STREAMS = ("kalshi_ws", "kalshi_rest", "odds")
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS
PAPER_EVERY_S = 300
TAPE_EVERY_S = 3600
GAME_WINDOW = {"nfl": (4 * HOUR_MS, int(3.5 * HOUR_MS)), "nba": (4 * HOUR_MS, int(2.75 * HOUR_MS))}
HEARTBEAT_MS = MIN_MS
SILENCE_MS = 3 * HEARTBEAT_MS
_TS = re.compile(rb'"recv_ts_ms":(\d{13})')
_MARKER = b'"src":"recorder"'
_USAGE_LAST = re.compile(rb'"x-requests-last":"?(\d+)')
_LOG_HEARTBEAT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) INFO recorder_ws: last 60s: (\{.*\})")
_LOG_CYCLE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) INFO recorder_rest: cycle: (\d+) markets, (\d+) changed books")
# Written on the laptop by the pull service (tape/pull.py) when the recorders run on EC2.
EC2_NOW = "ec2-now.json"
LAST_PULL = "last-pull.json"
EC2_FRESH_MS = 3 * MIN_MS
SCAN_VERSION = 3


def now_ms() -> int:
    return int(time.time() * 1000)


def et_day(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, ET).date().isoformat()


def et_midnight(day: str) -> int:
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


# ---------- tape file scanning ----------

def scan_file(path: Path, want_events: bool, want_usage: bool) -> dict:
    """One pass over a tape file at C speed: count records, collect active minutes, recorder events and
    odds usage. Never parses whole records."""
    opener = gzip.open if path.suffix == ".gz" else open
    records, minutes, events, usage = 0, set(), [], []
    first = last = None
    tail = b""
    with opener(path, "rb") as fh:
        while True:
            chunk = fh.read(4 << 20)
            if not chunk:
                break
            buf = tail + chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                tail = buf
                continue
            body, tail = buf[: cut + 1], buf[cut + 1 :]
            records += body.count(b"\n")
            for m in _TS.finditer(body):
                ts = int(m.group(1))
                minutes.add(ts // MIN_MS)
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
            if want_events:
                pos = body.find(_MARKER)
                while pos >= 0:
                    start = body.rfind(b"\n", 0, pos) + 1
                    end = body.find(b"\n", pos)
                    try:
                        rec = json.loads(body[start:end])
                        if rec.get("type") in ("_connected", "_disconnected", "_gap", "_heartbeat"):
                            events.append([rec["recv_ts_ms"], rec["type"]])
                    except (ValueError, KeyError):
                        pass
                    pos = body.find(_MARKER, end)
            if want_usage:
                for line in body.split(b"\n"):
                    if b'"src":"odds_api"' in line:
                        ts = _TS.search(line)
                        used = _USAGE_LAST.search(line)
                        if ts and used:
                            usage.append([int(ts.group(1)), int(used.group(1))])
    return {"records": records, "minutes": sorted(minutes), "first": first, "last": last, "events": events, "usage": usage}


class TapeIndex:
    """Per-file scan results, cached on disk and keyed by (size, mtime) so closed hours are read once."""

    def __init__(self, data: Path, cache_path: Path):
        self.data = data
        self.cache_path = cache_path
        try:
            cache = json.loads(cache_path.read_text())
            self.files = cache.get("files", {}) if cache.get("version") == SCAN_VERSION else {}
        except (OSError, ValueError):
            self.files = {}

    def refresh(self) -> dict[str, list[dict]]:
        seen, by_stream = set(), {s: [] for s in STREAMS}
        for stream in STREAMS:
            base = self.data / "raw" / stream
            if not base.exists():
                continue
            for day in sorted(p for p in base.iterdir() if p.is_dir()):
                hours: dict[str, Path] = {}
                for f in day.iterdir():
                    if f.name.endswith(".jsonl.gz"):
                        hours.setdefault(f.name[:2], f)
                    elif f.name.endswith(".jsonl"):
                        hours[f.name[:2]] = f  # plain wins while compression is in progress
                for hh, f in sorted(hours.items()):
                    rel = str(f.relative_to(self.data))
                    seen.add(rel)
                    try:
                        st = f.stat()
                    except FileNotFoundError:
                        continue
                    key = [st.st_size, int(st.st_mtime)]
                    entry = self.files.get(rel)
                    if not entry or entry.get("key") != key:
                        try:
                            res = scan_file(f, stream == "kalshi_ws", stream == "odds")
                        except (OSError, EOFError, gzip.BadGzipFile) as exc:  # being rotated right now
                            log.debug("skip %s: %r", rel, exc)
                            continue
                        entry = {"key": key, "bytes": st.st_size, **res}
                        self.files[rel] = entry
                    hour_start = int(dt.datetime.fromisoformat(f"{day.name}T{hh}:00:00+00:00").timestamp() * 1000)
                    by_stream[stream].append({"rel": rel, "hour_ms": hour_start, **entry})
        self.files = {k: v for k, v in self.files.items() if k in seen}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": SCAN_VERSION, "files": self.files}, separators=(",", ":")))
        os.replace(tmp, self.cache_path)
        return by_stream


def per_day(by_stream: dict[str, list[dict]], days: int, now: int) -> list[dict]:
    """Bytes on disk and records per ET day per stream, most recent `days` days, oldest first."""
    cutoff = et_day(now - (days - 1) * DAY_MS)
    table: dict[str, dict] = {}
    for stream, files in by_stream.items():
        for f in files:
            day = et_day(f["hour_ms"])
            if day < cutoff:
                continue
            row = table.setdefault(day, {"day": day, **{s: {"bytes": 0, "records": 0} for s in STREAMS}})
            row[stream]["bytes"] += f["bytes"]
            row[stream]["records"] += f["records"]
    out, d = [], dt.date.fromisoformat(cutoff)
    end = dt.date.fromisoformat(et_day(now))
    while d <= end:
        key = d.isoformat()
        out.append(table.get(key, {"day": key, **{s: {"bytes": 0, "records": 0} for s in STREAMS}}))
        d += dt.timedelta(days=1)
    return out


# ---------- recorder uptime and blind spots ----------

def _intervals_union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _clip(intervals: list[tuple[int, int]], a: int, b: int) -> list[tuple[int, int]]:
    return [(max(x, a), min(y, b)) for x, y in intervals if min(y, b) > max(x, a)]


def _overlap(intervals: list[tuple[int, int]], spans: list[tuple[int, int]]) -> int:
    total = 0
    for x, y in intervals:
        for a, b in spans:
            total += max(0, min(y, b) - max(x, a))
    return total


def log_heartbeats(log_path: Path, since_ms: int = 0) -> list[int]:
    """Timestamps of the live recorder's once-a-minute status lines (written by every connected session,
    even when no market moves). Times in the log are local time: the Mac's, or the EC2 recorder's, which
    infra/setup.sh sets to Eastern to match."""
    out = []
    try:
        with log_path.open("r", errors="replace") as fh:
            for line in fh:
                m = _LOG_HEARTBEAT.match(line)
                if m:
                    t = int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000) + int(m.group(2))
                    if t >= since_ms:
                        out.append(t)
    except FileNotFoundError:
        pass
    return out


def last_minute(log_path: Path) -> dict | None:
    """The live recorder's latest once-a-minute counts: messages, book deltas, trades, gaps, errors."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16_384))
            lines = fh.read().decode(errors="replace").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        m = _LOG_HEARTBEAT.match(line)
        if m:
            try:
                counts = ast.literal_eval(m.group(3))
            except (ValueError, SyntaxError):
                return None
            t = int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000) + int(m.group(2))
            return {"ts_ms": t, **{k: v for k, v in counts.items() if isinstance(v, (int, float))}}
    return None


def blind_spots(ws_files: list[dict], heartbeats: list[int], now: int) -> list[tuple[int, int]]:
    """When the live recorder could not see the market, over the whole tape: from each `_disconnected`
    to the next `_connected`, and any stretch over three minutes with no sign of life (no heartbeat,
    event or message: process stopped, Mac asleep, network gone). Quiet markets alone never last that
    long across every subscribed market, so silence this long means the recorder was blind."""
    events: list[list] = sorted(e for f in ws_files for e in f["events"])
    evidence = sorted(set(heartbeats) | {e[0] for e in events} | {m * MIN_MS for f in ws_files for m in f["minutes"]})
    if not evidence:
        return []
    down: list[tuple[int, int]] = []
    pending = None
    for ts, kind in events:
        if kind == "_disconnected" and pending is None:
            pending = ts
        elif kind == "_connected" and pending is not None:
            down.append((pending, ts))
            pending = None
    if pending is not None:
        down.append((pending, now))
    for a, b in zip(evidence, evidence[1:]):
        if b - a > SILENCE_MS:
            down.append((a + HEARTBEAT_MS, b))
    if now - evidence[-1] > SILENCE_MS:
        down.append((evidence[-1] + HEARTBEAT_MS, now))
    return _intervals_union(down)


def recent_blind_spots(heartbeats: list[int], since: int, now: int) -> list[tuple[int, int]]:
    """Blind spots after the last tape scan, from the recorder log's heartbeats alone (cheap enough to
    run every few minutes)."""
    beats = [t for t in heartbeats if t >= since - SILENCE_MS]
    if not beats:
        return [(since, now)] if now - since > SILENCE_MS else []
    down = [(a + HEARTBEAT_MS, b) for a, b in zip(beats, beats[1:]) if b - a > SILENCE_MS]
    if now - beats[-1] > SILENCE_MS:
        down.append((beats[-1] + HEARTBEAT_MS, now))
    return _clip(down, since, now)


def game_windows(games: list[dict]) -> list[tuple[int, int, str]]:
    out = []
    for g in games:
        k = g.get("kickoff_ms")
        if k:
            before, after = GAME_WINDOW.get(g.get("league") or "nfl", GAME_WINDOW["nfl"])
            out.append((k - before, k + after, g.get("label") or g.get("key")))
    return out


def uptime(down: list[tuple[int, int]], measured_from: int | None, games: list[dict], now: int, days: int = 7) -> dict:
    """Uptime over the last `days` days, measured only since the recorder's heartbeat existed (before
    it, a silent quiet night and a sleeping Mac look the same)."""
    if measured_from is None:
        return {"available": False, "reason": "no recorder heartbeats yet"}
    start, end = max(measured_from, now - days * DAY_MS), now
    span = max(1, end - start)
    d = _clip(down, start, end)
    down_ms = sum(b - a for a, b in d)
    longest = max(d, key=lambda iv: iv[1] - iv[0], default=None)
    windows = _clip(_intervals_union([(a, b) for a, b, _ in game_windows(games)]), start, end)
    game_ms = sum(b - a for a, b in windows)
    game_down = _overlap(d, windows)
    return {
        "available": True, "from_ms": start, "to_ms": end, "uptime": 1 - down_ms / span, "span_ms": span, "down_ms": down_ms,
        "game_uptime": (1 - game_down / game_ms) if game_ms else None, "game_ms": game_ms, "game_down_ms": game_down,
        "longest_outage_ms": (longest[1] - longest[0]) if longest else 0, "longest_outage_at": longest[0] if longest else None,
        "outages": len(d),
    }


def uptime_by_day(down: list[tuple[int, int]], measured_from: int | None, now: int, days: int = 14) -> dict[str, float | None]:
    out = {}
    for i in range(days - 1, -1, -1):
        day = et_day(now - i * DAY_MS)
        a, b = et_midnight(day), min(et_midnight(day) + DAY_MS, now)
        if measured_from is not None:
            a = max(a, measured_from)
        if measured_from is None or b - a < HOUR_MS:
            out[day] = None
            continue
        out[day] = 1 - sum(y - x for x, y in _clip(down, a, b)) / (b - a)
    return out


def odds_burn(odds_files: list[dict], now: int, days: int = 7) -> dict:
    samples = sorted(s for f in odds_files for s in f.get("usage", []))
    if not samples:
        return {"available": False}
    since = now - days * DAY_MS
    recent = [s for s in samples if s[0] >= since]
    span_days = max(1 / 24, (now - max(since, samples[0][0])) / DAY_MS)
    used = sum(c for _, c in recent)
    return {"available": True, "calls_7d": len(recent), "credits_7d": used, "per_day": used / span_days,
            "projected_30d": round(used / span_days * 30), "span_days": round(span_days, 2)}


def mapping_coverage(games: list[dict], now: int, days: int = 7) -> dict:
    recent = [g for g in games if g.get("kickoff_ms") and now - days * DAY_MS <= g["kickoff_ms"] <= now + 7 * DAY_MS]
    mapped = sum(1 for g in recent if g.get("odds_event_id"))
    return {"games": len(recent), "mapped": mapped, "share": (mapped / len(recent)) if recent else None}


# ---------- the board database ----------

def _ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def games_list(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _ro(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT key, league, label, kickoff_ms, odds_event_id FROM games")]
    finally:
        conn.close()


def market_activity(db_path: Path) -> dict:
    """Trades and contracts per ET day, and per game, from every public trade recorded."""
    if not db_path.exists():
        return {"days": {}, "games": {}}
    conn = _ro(db_path)
    try:
        days: dict[str, list] = {}
        for h, n, qty in conn.execute("SELECT ts_ms / 3600000, COUNT(*), COALESCE(SUM(qty), 0) FROM trades GROUP BY 1"):
            row = days.setdefault(et_day(h * HOUR_MS), [0, 0.0])
            row[0] += n
            row[1] += qty
        games = {}
        for key, markets in conn.execute("SELECT game_key, COUNT(*) FROM markets GROUP BY 1"):
            games[key] = {"markets": markets, "trades": 0, "contracts": 0.0}
        for key, n, qty in conn.execute(
                "SELECT m.game_key, COUNT(*), COALESCE(SUM(t.qty), 0) FROM trades t JOIN markets m USING (ticker) GROUP BY 1"):
            games.setdefault(key, {"markets": 0})
            games[key].update(trades=n, contracts=round(qty))
        return {"days": {d: {"trades": n, "contracts": round(q)} for d, (n, q) in days.items()}, "games": games}
    finally:
        conn.close()


class BookQuality:
    """Recorded book states per hour, and how many were crossed or locked (bid >= ask: a book that can't
    exist on the exchange, so the recorder or indexer missed an update). Reads only rows added since the
    last call, by rowid, so it stays cheap as the tape grows; starts over if board.db was rebuilt."""

    def __init__(self):
        self.rowid = 0
        self.hours: dict[int, list[int]] = {}

    def update(self, db_path: Path) -> dict[str, dict]:
        if not db_path.exists():
            return {}
        conn = _ro(db_path)
        try:
            top = conn.execute("SELECT MAX(rowid) FROM top_of_book").fetchone()[0] or 0
            if top < self.rowid:
                self.rowid, self.hours = 0, {}
            for h, n, crossed in conn.execute(
                    "SELECT ts_ms / 3600000, COUNT(*), SUM(bid >= ask) FROM top_of_book WHERE rowid > ? AND rowid <= ? "
                    "AND bid IS NOT NULL AND ask IS NOT NULL GROUP BY 1", (self.rowid, top)):
                row = self.hours.setdefault(h, [0, 0])
                row[0] += n
                row[1] += crossed or 0
            self.rowid = top
        finally:
            conn.close()
        days: dict[str, dict] = {}
        for h, (n, crossed) in self.hours.items():
            d = days.setdefault(et_day(h * HOUR_MS), {"books": 0, "crossed": 0})
            d["books"] += n
            d["crossed"] += crossed
        return days


def live_edges(db_path: Path, now: int, limit: int = 12) -> dict:
    """Every market before kickoff with a Pinnacle fair value, ranked by what taking it would be worth
    right now after the taker fee (the same test the paper take rule applies once a minute)."""
    if not db_path.exists():
        return {"matched": 0, "rows": []}
    conn = _ro(db_path)
    try:
        rows = conn.execute(
            "SELECT s.ticker, m.kind, m.label, g.key AS game_key, g.label AS game, g.kickoff_ms, s.bid, s.ask, s.bid_qty, "
            "s.ask_qty, s.fair, s.fair_ms, s.updated_ms FROM market_state s JOIN markets m USING (ticker) "
            "JOIN games g ON g.key = m.game_key WHERE s.fair IS NOT NULL AND g.kickoff_ms > ?", (now,)).fetchall()
        poll = conn.execute("SELECT last_ms FROM health WHERE stream = 'odds'").fetchone()
    finally:
        conn.close()
    poll_ms = poll[0] if poll else None
    out = []
    for r in rows:
        if r["bid"] is not None and r["ask"] is not None and r["bid"] >= r["ask"] - paper.EPS:
            continue  # crossed or locked: a missed update, not a price
        best = max(paper._take_candidates(r["fair"], r["bid"], r["ask"], r["bid_qty"], r["ask_qty"]), default=None)
        if best is None:
            continue
        edge, side, price, qty, _, _ = best
        mid = (r["bid"] + r["ask"]) / 2 if r["bid"] is not None and r["ask"] is not None else None
        # The paper rules' test: Pinnacle had refreshed the line when we got it (the other test, that we
        # got it recently, is the same for every row; see "stale" below).
        pulled = bool(poll_ms and r["fair_ms"] and poll_ms - r["fair_ms"] > paper.PIN_STALE_MS)
        out.append({"ticker": r["ticker"], "kind": r["kind"], "label": r["label"], "game_key": r["game_key"], "game": r["game"],
                    "kickoff_ms": r["kickoff_ms"], "bid": r["bid"], "ask": r["ask"], "fair": r["fair"], "fair_ms": r["fair_ms"],
                    "side": side, "price": price, "qty": qty, "edge": round(edge, 5), "gap": None if mid is None else round(r["fair"] - mid, 5),
                    "pulled": pulled})
    out.sort(key=lambda e: -e["edge"])
    take = paper.PRIMARY["take"]
    stale = not poll_ms or now - poll_ms > paper.TRADE_FRESH_MS
    return {"matched": len(rows), "threshold": take, "poll_ms": poll_ms, "stale": stale, "fresh_min": paper.TRADE_FRESH_MS // MIN_MS,
            "takeable": 0 if stale else sum(1 for e in out if e["edge"] >= take - paper.EPS and not e["pulled"]), "rows": out[:limit]}


def upcoming(db_path: Path, now: int, limit: int = 8) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _ro(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT g.key, g.league, g.label, g.kickoff_ms, COUNT(m.ticker) AS markets, "
            "SUM(CASE WHEN s.fair IS NOT NULL THEN 1 ELSE 0 END) AS matched, COALESCE(SUM(s.volume_today), 0) AS volume_today "
            "FROM games g LEFT JOIN markets m ON m.game_key = g.key LEFT JOIN market_state s USING (ticker) "
            "WHERE g.kickoff_ms > ? GROUP BY g.key ORDER BY g.kickoff_ms LIMIT ?", (now - 4 * HOUR_MS, limit))]
    finally:
        conn.close()


def health(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _ro(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM health ORDER BY stream")]
    finally:
        conn.close()


# ---------- cheap, per request ----------

def last_cycle(log_path: Path) -> dict | None:
    """The backup recorder's latest polling cycle: markets polled and books that changed."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16_384))
            lines = fh.read().decode(errors="replace").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        m = _LOG_CYCLE.match(line)
        if m:
            t = int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000) + int(m.group(2))
            return {"ts_ms": t, "markets": int(m.group(3)), "changed": int(m.group(4))}
    return None


def recorder_report(data: Path) -> dict:
    """What the recorders' machine knows about them right now. On EC2, the pull service fetches this
    every minute (`python -m board.tracker report`), since the laptop can't see those processes."""
    from tape.ctl import RECORDERS

    usage = shutil.disk_usage(data)
    return {
        "ts_ms": now_ms(),
        "services": [s for s in services_status(data) if s["name"] in RECORDERS],
        "recorder": last_minute(data / "logs" / "kalshi-live.log"),
        "backup": last_cycle(data / "logs" / "kalshi-backup.log"),
        "odds": latest_odds_usage(data),
        "disk": {"free": usage.free, "total": usage.total},
    }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def ec2_report(data: Path, now: int) -> tuple[dict | None, bool]:
    """The EC2 recorders' latest report and whether it is recent enough to trust."""
    report = _read_json(data / "run" / EC2_NOW)
    return report, bool(report) and now - report.get("ts_ms", 0) <= EC2_FRESH_MS


def known_until(data: Path, now: int) -> int:
    """How far this machine can judge whether the recorder could see. Locally, up to now. With the
    recorders on EC2, up to the last successful copy of the server's logs: after it, missing
    heartbeats mean "not copied yet", not "blind", so those minutes count neither way."""
    from tape.ctl import REMOTE_RECORDER

    if not REMOTE_RECORDER.exists():
        return now
    ok_ms = (_read_json(data / "run" / LAST_PULL) or {}).get("ok_ms")
    return min(now, ok_ms) if ok_ms else now


def now_services(data: Path, now: int) -> dict:
    """The Now section's service rows. With the recorders on EC2 their rows, the live recorder's last
    minute, odds credits and disk come from the server's latest report; a report over 3 minutes old
    leaves their state unknown rather than guessed."""
    from tape.ctl import RECORDERS, REMOTE_RECORDER

    local = services_status(data)
    usage = shutil.disk_usage(data)
    disk = {"free": usage.free, "total": usage.total, "where": "this Mac"}
    if not REMOTE_RECORDER.exists():
        return {"services": [s for s in local if s["name"] != "pull"], "recorder": last_minute(data / "logs" / "kalshi-live.log"),
                "backup": None, "odds": latest_odds_usage(data), "disk": disk, "remote": None}
    report, fresh = ec2_report(data, now)
    by_name = {s["name"]: s for s in (report or {}).get("services", [])}
    rows = []
    for name in RECORDERS:
        row = {"name": name, "stream": RECORDERS[name]["stream"], "running": False, "started_ms": None, "last_data_ms": None, "url": None,
               **by_name.get(name, {}), "where": "EC2"}
        if not fresh:
            row["running"] = None
        rows.append(row)
    pull = _read_json(data / "run" / LAST_PULL)
    for s in local:
        if s["name"] not in RECORDERS:
            rows.append({**s, "last_pull": pull} if s["name"] == "pull" else s)
    report = report or {}
    return {
        "services": rows,
        "recorder": report.get("recorder"),
        "backup": report.get("backup"),
        "odds": report.get("odds"),
        "disk": {**report["disk"], "where": "EC2"} if report.get("disk") else disk,
        "remote": {"reported_ms": report.get("ts_ms"), "fresh": fresh},
    }


def services_status(data: Path) -> list[dict]:
    from tape.ctl import SERVICES, _alive  # local import: ctl is a CLI module

    run = data / "run"
    out = []
    for name, spec in SERVICES.items():
        pid = None
        started = None
        pidfile = run / f"{name}.pid"
        try:
            pid = int(pidfile.read_text().strip())
            started = int(pidfile.stat().st_mtime * 1000)
        except (OSError, ValueError):
            pass
        running = bool(pid) and _alive(pid)
        stream = spec.get("stream")
        out.append({"name": name, "stream": stream, "running": running, "started_ms": started if running else None,
                    "last_data_ms": newest_mtime_ms(data / "raw" / stream) if stream else None, "url": spec.get("url")})
    return out


def newest_mtime_ms(base: Path) -> int | None:
    if not base.exists():
        return None
    for day in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)[:2]:
        times = [f.stat().st_mtime for f in day.iterdir() if f.is_file()]
        if times:
            return int(max(times) * 1000)
    return None


def latest_odds_usage(data: Path) -> dict | None:
    base = data / "raw" / "odds"
    if not base.exists():
        return None
    for day in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)[:2]:
        for f in sorted(day.iterdir(), reverse=True):
            if f.suffix != ".jsonl":
                continue
            with f.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - (1 << 20)))
                lines = fh.read().split(b"\n")
            for line in reversed(lines):
                if b'"src":"odds_api"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                u = rec.get("usage") or {}
                return {"ts_ms": rec.get("recv_ts_ms"), "remaining": _int(u.get("x-requests-remaining")),
                        "used": _int(u.get("x-requests-used")), "last": _int(u.get("x-requests-last"))}
    return None


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# ---------- the background worker ----------

class Tracker:
    def __init__(self, data: Path, db_path: Path, fetch=fetch_markets):
        self.data = Path(data)
        self.db_path = Path(db_path)
        self.dir = self.data / "tracker"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.result_path = self.dir / "stats.json"
        self.log_path = self.data / "logs" / "kalshi-live.log"
        self.index = TapeIndex(self.data, self.dir / "tapefiles.json")
        self.paper = PaperBook(self.data, self.db_path, Settlements(self.dir / "settlements.json", fetch))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.refreshing = False
        self.force_tape = False
        self.error: str | None = None
        self.books = BookQuality()
        self.blind: list[tuple[int, int]] = []
        self.blind_until = 0  # blind spots are known from the tape up to here
        self.tape_at = 0.0
        try:
            self.result = json.loads(self.result_path.read_text())
        except (OSError, ValueError):
            self.result = {}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="tracker", daemon=True).start()

    def _loop(self) -> None:
        while True:
            self.refresh()
            self.wake.wait(timeout=PAPER_EVERY_S)
            self.wake.clear()

    def request_refresh(self) -> bool:
        """Rescan the tape and recompute now. A request during a recompute runs right after it (a pull
        can land mid-recompute, and its new hours must not wait for the hourly scan). True if this
        started a recompute, False if it was queued behind one."""
        busy = self.refreshing
        self.force_tape = True
        self.wake.set()
        return not busy

    def refresh(self) -> dict:
        self.refreshing = True
        started = time.monotonic()
        try:
            now = now_ms()
            known = known_until(self.data, now)
            result = dict(self.result)
            if self.force_tape or not self.blind_until or time.monotonic() - self.tape_at >= TAPE_EVERY_S:
                self.force_tape = False
                result["tape"] = self._tape(now, known)
                self.tape_at = time.monotonic()
            beats = log_heartbeats(self.log_path, self.blind_until - SILENCE_MS)
            blind = _intervals_union(self.blind + recent_blind_spots(beats, self.blind_until, known))
            t = time.monotonic()
            result["paper"] = self.paper.run(blind, now, now)
            result["paper_took_s"] = round(time.monotonic() - t, 1)
            result["computed_ms"] = now
            result["took_s"] = round(time.monotonic() - started, 1)
            with self.lock:
                self.result = result
                self.error = None
            tmp = self.result_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False))
            os.replace(tmp, self.result_path)
            log.info("tracker refreshed in %.1fs", result["took_s"])
        except Exception as exc:  # keep serving the last good numbers
            log.exception("tracker refresh failed")
            self.error = repr(exc)
        finally:
            self.refreshing = False
        return self.result

    def _tape(self, now: int, known: int | None = None) -> dict:
        known = now if known is None else known
        by_stream = self.index.refresh()
        heartbeats = log_heartbeats(self.log_path)
        ws = by_stream["kalshi_ws"]
        beats = sorted(set(heartbeats) | {e[0] for f in ws for e in f["events"] if e[1] == "_heartbeat"})
        self.blind = blind_spots(ws, beats, known)
        self.blind_until = known
        measured_from = beats[0] - HEARTBEAT_MS if beats else None
        games = games_list(self.db_path)
        week = now - 7 * DAY_MS
        return {
            "scanned_ms": now,
            "per_day": per_day(by_stream, 14, now),
            "uptime": uptime(self.blind, measured_from, games, known),
            "uptime_days": uptime_by_day(self.blind, measured_from, known),
            "known_until_ms": known,
            "timeline": {"from_ms": week, "to_ms": now, "measured_from": measured_from,
                         "down": [[a, b] for a, b in _clip(self.blind, week, now)],
                         "games": [[a, b, label] for a, b, label in game_windows(games) if b > week and a < now]},
            "activity": market_activity(self.db_path),
            "books": self.books.update(self.db_path),
            "mapping": mapping_coverage(games, now),
            "odds_burn": odds_burn(by_stream["odds"], now),
            "data_bytes": dir_size(self.data / "raw"),
            "db_bytes": sum(p.stat().st_size for p in self.data.glob("board.db*") if p.is_file()),
        }

    def snapshot(self) -> dict:
        with self.lock:
            result = dict(self.result)
        now = now_ms()
        rec = now_services(self.data, now)
        return {
            **result,
            "now_ms": now,
            "refreshing": self.refreshing,
            "error": self.error,
            "next_refresh_ms": (result.get("computed_ms") or now) + PAPER_EVERY_S * 1000,
            "now": {
                **rec,
                "health": health(self.db_path),
                "edges": live_edges(self.db_path, now),
                "upcoming": upcoming(self.db_path, now),
            },
        }


if __name__ == "__main__":
    import sys

    from tape.config import data_dir

    if sys.argv[1:] != ["report"]:
        raise SystemExit("usage: python -m board.tracker report   # the recorders' state as JSON (run on EC2)")
    print(json.dumps(recorder_report(data_dir()), separators=(",", ":")))
