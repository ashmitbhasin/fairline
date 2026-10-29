"""Keeps data/board.db current from the tape.

    python -m board.indexer            # resume (or build from scratch the first time), then follow live
    python -m board.indexer --rebuild  # throw the database away and rebuild it from the whole tape

Start-up replays tape hour by hour, merging the three streams by receive time so books, trades and
fair values are rebuilt in the order they happened. On a resume it replays only the last 45 minutes
(the WebSocket recorder reconnects every 30 minutes, so every book gets a fresh snapshot inside that
window) and does not re-insert history the database already holds. Then it follows the live files
and flushes changes every 0.25 s.

With the recorders on EC2 (infra/fl has set up a server) there are no live files here: infra/fl pull
copies finished hours down and records a frontier, the time before which every Kalshi stream is
complete (tape/manifest.py). The indexer then applies whole hours, all streams merged by receive
time, and never an hour at or past the frontier, so a stream whose file landed first cannot run
ahead of one still being copied.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import heapq
import json
import logging
from collections import deque
import signal
import time

from tape.config import data_dir

from . import db
from .state import ET, BoardState
from .tapefiles import StreamTailer, key_for_ms, next_key, read_records, stream_files

log = logging.getLogger("board.indexer")
STREAMS = ("kalshi_ws", "kalshi_rest", "odds")
RECORDER_NAME = {"kalshi_ws": "kalshi-live", "kalshi_rest": "kalshi-backup", "odds": "odds"}
RESUME_WINDOW_MS = 45 * 60_000
ODDS_RESUME_WINDOW_MS = 4 * 3600_000  # odds are polled hourly on the free tier
FRONTIER_FILE = "tape-frontier.json"   # in data/run/, written by infra/fl pull


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


FOLLOW_BATCH = 50_000


def take_aligned(buffers: dict[str, deque], caught_up: dict[str, bool]) -> dict[str, list[dict]]:
    """Records safe to apply now: up to the earliest receive time any stream with a backlog has
    reached. When tape arrives in hours at a time (copied down from EC2), a dense stream such as
    kalshi_ws takes longer to read than the others; without this the others would race ahead, the
    stored checkpoint (the latest receive time applied) would pass records not yet read, and a
    restart would skip them."""
    backlog = [buf[-1].get("recv_ts_ms") or 0 for s, buf in buffers.items() if buf and not caught_up[s]]
    frontier = min(backlog) if backlog else None
    out: dict[str, list[dict]] = {}
    for s, buf in buffers.items():
        take = []
        while buf and (frontier is None or (buf[0].get("recv_ts_ms") or 0) <= frontier):
            take.append(buf.popleft())
        out[s] = take
    return out


class _NoGC:
    """Applying a dense hour holds millions of records in memory at once, and Python 3.14's collector
    re-walks them all repeatedly (a whole-tape rebuild stalled in it for minutes per hour). Applying
    records creates no reference cycles, so collection waits until the batch is done."""

    def __enter__(self):
        self.was = gc.isenabled()
        gc.disable()

    def __exit__(self, *exc):
        if self.was:
            gc.enable()


def remote_tape() -> bool:
    """True when the recorders run on EC2 and the tape here is copied down by infra/fl pull."""
    try:
        from tape import ctl
    except Exception:
        return False
    return ctl.REMOTE_RECORDER.exists()


def tape_frontier(root) -> int | None:
    """EC2 mode: the receive time before which every Kalshi stream's tape is complete here."""
    try:
        return int(json.loads((root / "run" / FRONTIER_FILE).read_text())["frontier_ms"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def recorder_running(stream: str) -> bool | None:
    """Running, stopped, or None when unknown. With the recorders on EC2 this is the server's latest
    report (fetched every minute by the pull service), not a process on this machine."""
    try:
        from tape import ctl
    except Exception:
        return None
    name = RECORDER_NAME[stream]
    spec = ctl.RECORDERS.get(name)
    if spec is None:
        return None
    if ctl.REMOTE_RECORDER.exists():
        from .tracker import ec2_report

        report, fresh = ec2_report(data_dir(), int(time.time() * 1000))
        if not fresh:
            return None
        return any(s.get("name") == name and s.get("running") for s in report.get("services", []))
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
        self.remote = remote_tape()
        self.applied: set[tuple[str, tuple[str, str]]] = set()  # (stream, hour) already applied

    # ---- start-up replay -----------------------------------------------------------

    def limit_key(self):
        """Replay and apply only hours before this key: the current hour locally (the recorders are
        still writing it), the copied frontier with the recorders on EC2 (None: nothing copied yet)."""
        if not self.remote:
            return key_for_ms(int(time.time() * 1000))
        frontier = tape_frontier(self.root)
        return key_for_ms(frontier) if frontier else None

    def replay(self) -> dict:
        limit = self.limit_key()
        starts = {s: None for s in STREAMS}
        if self.last_ts:
            starts = {s: key_for_ms(self.last_ts - (ODDS_RESUME_WINDOW_MS if s == "odds" else RESUME_WINDOW_MS)) for s in STREAMS}
        self.floor = dict(starts)
        files = {s: {k: p for k, p in stream_files(self.root, s).items()
                     if limit is not None and k < limit and (starts[s] is None or k >= starts[s])} for s in STREAMS}
        hours = sorted(set().union(*(f.keys() for f in files.values())))
        mode = f"resuming from {dt.datetime.fromtimestamp(self.last_ts / 1000, ET):%a %H:%M}" if self.last_ts else "building from the whole tape"
        log.info("%s: %d closed hour(s) to replay", mode, len(hours))
        with _NoGC():
            self._replay_hours(files, hours, time.time())
        return {s: next_key(max(files[s])) if files[s] else starts[s] for s in STREAMS}

    def _replay_hours(self, files: dict, hours: list, started: float) -> None:
        count = bad = 0
        for i, hour in enumerate(hours, 1):
            batches = {}
            for s in STREAMS:
                if hour in files[s]:
                    batches[s], n_bad = read_records(files[s][hour])
                    bad += n_bad
                    self.applied.add((s, hour))
            for stream, rec in merged(batches):
                apply(self.state, stream, rec)
                count += 1
            self.writer.flush(self.state, self.state.drain())
            if i % 6 == 0 or i == len(hours):
                log.info("  replayed %s %s:00 UTC (%d/%d hours, %d records, %.0fs)", hour[0], hour[1], i, len(hours), count, time.time() - started)
        if bad:
            log.warning("skipped %d unreadable tape lines during replay", bad)

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

    def apply_copied(self) -> int:
        """EC2 mode: apply every copied hour before the frontier not applied yet, oldest first, each
        hour's streams merged by receive time. Returns the number of hours applied."""
        frontier = tape_frontier(self.root)  # read once: a pull may move it while this runs
        if frontier is None:
            return 0
        limit = key_for_ms(frontier)
        files = {s: {k: p for k, p in stream_files(self.root, s).items()
                     if k < limit and (s, k) not in self.applied and (self.floor.get(s) is None or k >= self.floor[s])}
                 for s in STREAMS}
        hours = sorted(set().union(*(f.keys() for f in files.values())))
        with _NoGC():
            self._apply_hours(files, hours)
        if hours:
            log.info("applied %d copied hour(s), through %s %s:00 UTC", len(hours), hours[-1][0], hours[-1][1])
        done = int(db.get_meta(self.conn, "complete_until_ms") or 0)
        if frontier > done:
            with self.conn:
                db.set_meta(self.conn, "complete_until_ms", frontier)  # read by the tracker's paper record
        return len(hours)

    def _apply_hours(self, files: dict, hours: list) -> None:
        for hour in hours:
            batches = {}
            for s in STREAMS:
                if hour in files[s]:
                    batches[s], n_bad = read_records(files[s][hour])
                    if n_bad:
                        log.warning("%s %s %s:00: skipped %d unreadable lines", s, hour[0], hour[1], n_bad)
                    self.applied.add((s, hour))
            for stream, rec in merged(batches):
                apply(self.state, stream, rec)
            self.writer.flush(self.state, self.state.drain())

    def follow_copied(self) -> None:
        """EC2 mode: wait for infra/fl pull to copy more hours, and apply them."""
        last_check = last_health = last_procs = 0.0
        log.info("following copied tape (recorders on EC2): applying hours as the frontier passes them")
        while not self.stop:
            now = time.time()
            if now - last_check >= 5:
                self.apply_copied()
                last_check = now
            if now - last_procs >= 5:
                self.running = {s: recorder_running(s) for s in STREAMS}
                last_procs = now
            if now - last_health >= 1:
                self.writer.write_health(self.health_rows(int(now * 1000)))
                last_health = now
            time.sleep(min(self.interval * 4, 1.0))

    def follow(self, starts: dict) -> None:
        tailers = {s: StreamTailer(self.root, s, start=starts[s]) for s in STREAMS}
        buffers: dict[str, deque] = {s: deque() for s in STREAMS}
        last_flush = last_health = last_procs = 0.0
        log.info("live: following %s", ", ".join(STREAMS))
        while not self.stop:
            caught_up = {}
            for s, t in tailers.items():
                want = FOLLOW_BATCH - len(buffers[s])
                got = t.poll(want) if want > 0 else []
                buffers[s].extend(got)
                caught_up[s] = want > 0 and len(got) < want
            batches = take_aligned(buffers, caught_up)
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
        if self.remote:
            self.follow_copied()
        else:
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
