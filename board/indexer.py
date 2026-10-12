"""Keeps data/board.db current from the tape.

    python -m board.indexer            # resume (or build from scratch the first time), then follow live
    python -m board.indexer --rebuild  # throw the database away and rebuild it from the whole tape

Start-up replays tape hour by hour, merging the three streams by receive time so books, trades and
fair values are rebuilt in the order they happened. On a resume it replays only the last 45 minutes
(the WebSocket recorder reconnects every 30 minutes, so every book gets a fresh snapshot inside that
window) and does not re-insert history the database already holds. Then it follows the live files
and flushes changes every 0.25 s.
"""
from __future__ import annotations

import argparse
import datetime as dt
import heapq
import logging
import signal
import time

from tape.config import data_dir

from . import db
from .state import ET, BoardState
from .tapefiles import StreamTailer, key_for_ms, read_records, stream_files

log = logging.getLogger("board.indexer")
STREAMS = ("kalshi_ws", "kalshi_rest", "odds")
RECORDER_NAME = {"kalshi_ws": "kalshi-live", "kalshi_rest": "kalshi-backup", "odds": "odds"}
RESUME_WINDOW_MS = 45 * 60_000
ODDS_RESUME_WINDOW_MS = 4 * 3600_000  # odds are polled hourly on the free tier


def apply(state: BoardState, stream: str, rec: dict) -> None:
    if stream == "kalshi_ws":
        state.apply_ws(rec)
    elif stream == "kalshi_rest":
        state.apply_rest(rec)
    else:
        state.apply_odds(rec)


def merged(batches: dict[str, list[dict]]):
    """Records from several streams in receive-time order (each batch is already in order)."""
    def keyed(stream, recs):
        for i, r in enumerate(recs):
            yield (r.get("recv_ts_ms") or 0, stream, i, r)
    for ts, stream, _, rec in heapq.merge(*(keyed(s, recs) for s, recs in batches.items())):
        yield stream, rec


def recorder_running(stream: str) -> bool | None:
    try:
        from tape import ctl
    except Exception:
        return None
    name = RECORDER_NAME[stream]
    spec = ctl.RECORDERS.get(name)
    if spec is None:
        return None
    return bool(ctl.read_pid(name) or ctl.stray_pids(spec["module"], None))


class Indexer:
    def __init__(self, rebuild: bool = False, interval: float = 0.25):
        root = data_dir()
        self.root = root
        self.interval = interval
        path = root / "board.db"
        if rebuild:
            for suffix in ("", "-wal", "-shm"):
                (root / f"board.db{suffix}").unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(path)
        db.init(self.conn)
        if db.get_meta(self.conn, "schema_version") != db.SCHEMA_VERSION:
            raise SystemExit("board.db was built by a different version; run with --rebuild")
        self.writer = db.Writer(self.conn)
        last = db.get_meta(self.conn, "last_ts_ms")
        self.last_ts = int(last) if last else None
        self.state = BoardState(history_after_ms=self.last_ts or 0)
        if self.last_ts:
            db.load_catalog(self.conn, self.state)
        self.running: dict[str, bool | None] = {}
        self.stop = False

    # ---- start-up replay -----------------------------------------------------------

    def replay(self) -> dict:
        now_key = key_for_ms(int(time.time() * 1000))
        starts = {s: None for s in STREAMS}
        if self.last_ts:
            starts = {s: key_for_ms(self.last_ts - (ODDS_RESUME_WINDOW_MS if s == "odds" else RESUME_WINDOW_MS)) for s in STREAMS}
        files = {s: {k: p for k, p in stream_files(self.root, s).items() if k < now_key and (starts[s] is None or k >= starts[s])} for s in STREAMS}
        hours = sorted(set().union(*(f.keys() for f in files.values())))
        mode = f"resuming from {dt.datetime.fromtimestamp(self.last_ts / 1000, ET):%a %H:%M}" if self.last_ts else "building from the whole tape"
        log.info("%s: %d closed hour(s) to replay", mode, len(hours))
        started, count = time.time(), 0
        bad = 0
        for i, hour in enumerate(hours, 1):
            batches = {}
            for s in STREAMS:
                if hour in files[s]:
                    batches[s], n_bad = read_records(files[s][hour])
                    bad += n_bad
            for stream, rec in merged(batches):
                apply(self.state, stream, rec)
                count += 1
            self.writer.flush(self.state, self.state.drain())
            if i % 6 == 0 or i == len(hours):
                log.info("  replayed %s %s:00 UTC (%d/%d hours, %d records, %.0fs)", hour[0], hour[1], i, len(hours), count, time.time() - started)
        if bad:
            log.warning("skipped %d unreadable tape lines during replay", bad)
        return {s: now_key for s in STREAMS}

    def finish_resume(self) -> None:
        now = dt.datetime.now(ET)
        day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        db.load_today(self.conn, self.state, now.date().isoformat(), day_start)
        self.writer.flush(self.state, self.state.drain())

    # ---- live --------------------------------------------------------------------

    def health_rows(self, now_ms: int) -> list[tuple]:
        rows = []
        for stream, h in self.state.health.items():
            rows.append((stream, now_ms, h.last_ms, h.per_minute(now_ms), None if self.running.get(stream) is None else int(self.running[stream]),
                         None if h.connected is None else int(h.connected), h.gaps_today, h.disconnects_today, h.credits, h.last_error))
        return rows

    def follow(self, starts: dict) -> None:
        tailers = {s: StreamTailer(self.root, s, start=starts[s]) for s in STREAMS}
        last_flush = last_health = last_procs = 0.0
        log.info("live: following %s", ", ".join(STREAMS))
        while not self.stop:
            batches = {s: t.poll(50_000) for s, t in tailers.items()}
            n = 0
            for stream, rec in merged(batches):
                apply(self.state, stream, rec)
                n += 1
            now = time.time()
            if now - last_flush >= self.interval:
                if self.state.dirty or self.state.depth_dirty or self.state.meta_dirty or self.state.game_dirty or self.state.tape_rows:
                    self.writer.flush(self.state, self.state.drain())
                last_flush = now
            if now - last_procs >= 5:
                self.running = {s: recorder_running(s) for s in STREAMS}
                last_procs = now
            if now - last_health >= 1:
                self.writer.write_health(self.health_rows(int(now * 1000)))
                last_health = now
            if n == 0:
                time.sleep(self.interval)
        for t in tailers.values():
            t.close()

    def run(self) -> None:
        starts = self.replay()
        if self.last_ts:
            self.finish_resume()
        log.info("board ready: %d markets, %d games", len(self.state.markets), len(self.state.games))
        self.follow(starts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="delete board.db and rebuild it from the whole tape")
    parser.add_argument("--interval", type=float, default=0.25, help="seconds between database flushes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    indexer = Indexer(rebuild=args.rebuild, interval=args.interval)

    def _stop(*_):
        indexer.stop = True

    signal.signal(signal.SIGTERM, _stop)
    try:
        indexer.run()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.conn.close()


if __name__ == "__main__":
    main()
