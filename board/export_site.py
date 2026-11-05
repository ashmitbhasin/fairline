"""Write site/data.js: the figures the public project site (site/) shows.

    .venv/bin/python -m board.export_site      # needs the dashboard running (python -m tape.ctl start)

Aggregates only, by design: counts, uptime by day, the Pinnacle-vs-Kalshi gap by time to kickoff,
the paper record by rule, and the gate registry. No ticker, price, order book or odds quote leaves
this machine: Kalshi's Developer Agreement §3.1 bars redistributing its API data, so the site shows
what Fairline measured, never what it recorded.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import urllib.request
from pathlib import Path

import yaml

from tape.config import ROOT, data_dir

from .tracker import (
    ET, HEARTBEAT_MS, TapeIndex, blind_spots, known_until, log_heartbeats, per_day, uptime_by_day,
)

TRACKER_URL = "http://127.0.0.1:8765/api/tracker"
OUT = ROOT / "site" / "data.js"
OWNER = {"name": "Ashmit Bhasin", "email": "ashsinghbhasin@gmail.com"}

# Facts from the record, fixed in time (each is in the repo's history or the tracker as it stood then).
CUTOVER_MS = int(dt.datetime(2026, 10, 26, 18, 11, 45, tzinfo=ET).timestamp() * 1000)  # recorders start on EC2
G1_FIRST_READ = {"day": "2026-10-26", "uptime": 0.108, "game_uptime": 0.074, "verdict": "no-go",
                 "why": "The recorder ran on a laptop that slept."}
# Read on Nov 3 over the first seven days on the server (CUTOVER_MS to CUTOVER_MS + 7 days), with the tracker's
# uptime functions; G1b was registered that day for the NFL tape (research/registry/G1b-nfl-tape-integrity.yaml).
G1_SECOND_READ = {"day": "2026-11-03", "uptime": 0.99453, "game_uptime": 0.99386, "verdict": "open",
                  "label": "Open: no NBA tape yet",
                  "why": "The NFL tape passed (G1b). NBA was never recorded and no nflverse game id is stored."}
G1B_READ = {"day": "2026-11-03", "uptime": 0.99453, "game_uptime": 0.99386, "gaps": 0, "mapped": 1.0, "verdict": "go",
            "why": "First seven days on the server, Oct 26 18:11 to Nov 2 17:11 ET: 638 s blind in 28.8 game-window "
                   "hours, all at the planned 30-minute reconnect; no sequence gaps; 692 of 692 core markets mapped."}
# Looked at on Nov 4 with board/paper.py's minute replay, games grouped by NFL week of kickoff (Tue-Mon ET). G2's
# Weeks 6-8 can't be read (the laptop saw 17% and 9% of Weeks 6 and 7), so G2b reads the same bar on Weeks 8-10.
G2_READ = {"day": "2026-11-04", "verdict": "void", "label": "Can't be read: Weeks 6-7 unseen",
           "why": "The recorder saw 17% of Week 6's pregame minutes and 9% of Week 7's. Read as G2b on Weeks 8-10."}
G2B_LOOK = {"day": "2026-11-04", "verdict": "look", "label": "Week 8: 22.1 side-hours", "week": 8,
            "qualifying_hours": 22.1, "qualifying_hours_per_outcome": 13.0, "side_hours": 5734.6, "seen": 0.938,
            "mean_abs_gap": 0.0065, "why": "Week 8 only, before Weeks 9 and 10: below several dozen side-hours, with "
                                           "19.6 of the 22.1 from game-winner markets."}
READS = {"G1": [G1_FIRST_READ, G1_SECOND_READ], "G1b": [G1B_READ], "G2": [G2_READ], "G2b": [G2B_LOOK]}
NEXT_READ_MS = {"G2b": int(dt.datetime(2026, 11, 17, 12, tzinfo=ET).timestamp() * 1000)}  # Week 10 ends Mon Nov 16
NAIVE_REPLAY = {"day": "2026-10-15", "rule": "take@2", "won": 1, "lost": 0, "pnl": 57.0}  # commit ec5731e

NAMES = {"paper-maker-clv": "Paper maker CLV", "late-game-calibration": "Late-game calibration",
         "nfl-tape-integrity": "NFL tape integrity", "divergence-server-weeks": "Divergence, server weeks"}
VARIANT_FIELDS = ("id", "rule", "param", "primary", "bets", "settled", "won", "lost", "tied", "pending",
                  "pnl", "staked", "roi", "clv_n", "clv_mean", "clv_ci", "beat_close")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.load(resp)


def gate_rows(registry: Path) -> list[dict]:
    rows = []
    for path in sorted(registry.glob("G*.yaml")):
        g = yaml.safe_load(path.read_text())
        gid, _, slug = g["id"].partition("-")
        name = NAMES.get(slug, slug.replace("-", " ").capitalize())
        row = {"id": gid, "name": name, "registered": str(g["registered"]),
               "hypothesis": g["hypothesis"], "go": g["go"], "no_go": g.get("no_go"),
               "earliest_read": str(g.get("earliest_read", "")).split(" ")[0], "reads": list(READS.get(gid, []))}
        if gid in NEXT_READ_MS:
            row["next_read_ms"] = NEXT_READ_MS[gid]
        rows.append(row)
    return rows


def tape_summary(data: Path, now: int) -> dict:
    """Uptime and volume for every day recorded, from the tape and the recorder log, measured the way
    the tracker measures them (same functions), over the whole recording rather than two weeks."""
    index = TapeIndex(data, data / "tracker" / "site-tapefiles.json")  # its own cache: the tracker owns its
    by_stream = index.refresh()
    ws = by_stream["kalshi_ws"]
    beats = sorted(set(log_heartbeats(data / "logs" / "kalshi-live.log"))
                   | {e[0] for f in ws for e in f["events"] if e[1] == "_heartbeat"})
    known = known_until(data, now)
    blind = blind_spots(ws, beats, known)
    measured_from = beats[0] - HEARTBEAT_MS if beats else None
    first = min(f["hour_ms"] for f in ws)
    n_days = (dt.datetime.fromtimestamp(now / 1000, ET).date() - dt.datetime.fromtimestamp(first / 1000, ET).date()).days + 1
    up = uptime_by_day(blind, measured_from, known, days=n_days)
    counts = {r["day"]: r["kalshi_ws"]["records"] for r in per_day(by_stream, n_days, now)}
    since = [(max(a, CUTOVER_MS), min(b, known)) for a, b in blind if b > CUTOVER_MS and a < known]
    span = max(1, known - CUTOVER_MS)
    gaps = sum(1 for f in ws for e in f["events"] if e[1] == "_gap" and e[0] >= CUTOVER_MS)
    return {
        "days": [{"day": d, "uptime": up.get(d), "messages": counts.get(d, 0)} for d in sorted(up)],
        "cutover_ms": CUTOVER_MS,
        "measured_through_ms": known,
        "since_cutover": {"uptime": 1 - sum(b - a for a, b in since) / span, "blind_s": round(sum(b - a for a, b in since) / 1000),
                          "outages": len(since), "gaps": gaps, "hours": round(span / 3_600_000, 1)},
        "messages": sum(f["records"] for f in ws),
        "first_day": dt.datetime.fromtimestamp(first / 1000, ET).date().isoformat(),
    }


def build(data: Path, tracker: dict, now: int) -> dict:
    paper = tracker.get("paper") or {}
    g2 = paper.get("g2") or {}
    db_path = data / "board.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        games = conn.execute("SELECT COUNT(*) FROM games WHERE kickoff_ms <= ?", (now,)).fetchone()[0]
        markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    finally:
        conn.close()
    from fairvalue.fees import MAKER_COEF, TAKER_COEF
    gates = gate_rows(ROOT / "research" / "registry")
    return {
        "as_of_ms": now,
        "as_of": dt.datetime.fromtimestamp(now / 1000, ET).strftime("%b %-d, %Y, %-I:%M %p ET"),
        "owner": OWNER,
        "desk": {"games": games, "markets": markets, "matched_markets": g2.get("markets"),
                 "side_minutes": g2.get("side_minutes"), "gates": len(gates)},
        "tape": tape_summary(data, now),
        "kickoff": {
            "rows": (g2.get("by_lead") or {}).get("rows", []),
            "bin_min": (g2.get("by_lead") or {}).get("bin_min", 5),
            "median_gap": g2.get("median_gap"), "p90_gap": g2.get("p90_gap"),
            "qualifying_hours": g2.get("qualifying_hours"), "recent_hours": g2.get("recent_hours"),
            "taker_fee_at_50": TAKER_COEF * 0.25, "maker_fee_at_50": MAKER_COEF * 0.25, "g2_bar": 0.02,
        },
        "record": {
            "variants": [{k: v.get(k) for k in VARIANT_FIELDS} for v in paper.get("variants", [])],
            "params": paper.get("params"),
            "naive": NAIVE_REPLAY,
        },
        "gates": gates,
    }


def main() -> None:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    data = data_dir()
    out = build(data, _get_json(TRACKER_URL), now)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("// Generated by board/export_site.py. Aggregates only; see that file before editing.\n"
                   f"window.FAIRLINE_DATA = {json.dumps(out, separators=(',', ':'), allow_nan=False)};\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(out['kickoff']['rows'])} kickoff buckets, "
          f"{len(out['tape']['days'])} days, {len(out['record']['variants'])} rules, as of {out['as_of']}")


if __name__ == "__main__":
    main()
