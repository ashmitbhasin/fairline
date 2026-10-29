"""Reading and following tape files.

Recorders write <data>/raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl and gzip each hour once it closes.
StreamTailer reads a stream from any hour onward and then keeps following it live:

- It keeps the current file open, so it can finish reading an hour even after the recorder's
  compressor has replaced <HH>.jsonl with <HH>.jsonl.gz (an unlinked file stays readable).
- It moves to the next hour only after the recorder has started that hour, at which point the
  previous file is closed and complete.
- A partial last line (the writer flushes in chunks) is held back until its newline arrives.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
from pathlib import Path

log = logging.getLogger("board.tape")

Key = tuple[str, str]  # (YYYY-MM-DD, HH)


def stream_files(root: Path, stream: str) -> dict[Key, Path]:
    """Every hour file for a stream. If an hour exists as both .jsonl and .jsonl.gz (compression in
    progress), the .jsonl is complete and the .gz may not be, so the .jsonl wins."""
    base = Path(root) / "raw" / stream
    found: dict[Key, Path] = {}
    if not base.exists():
        return found
    for path in base.glob("*/*.jsonl*"):
        name = path.name
        if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
            continue
        key = (path.parent.name, name.split(".", 1)[0])
        if key not in found or name.endswith(".jsonl"):
            found[key] = path
    return found


def read_records(path: Path) -> tuple[list[dict], int]:
    """Every complete record in one closed hour file, plus the count of unreadable lines."""
    opener = gzip.open if path.name.endswith(".gz") else open
    records, bad = [], 0
    try:
        with opener(path, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    bad += 1
    except (EOFError, OSError) as exc:  # a truncated .gz: keep what was readable, and say so
        log.warning("%s: unreadable after %d records (%r)", path, len(records), exc)
        bad += 1
    return records, bad


def key_for_ms(ts_ms: int) -> Key:
    t = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    return (t.strftime("%Y-%m-%d"), t.strftime("%H"))


def next_key(key: Key) -> Key:
    t = dt.datetime.strptime(f"{key[0]} {key[1]}", "%Y-%m-%d %H").replace(tzinfo=dt.timezone.utc)
    return key_for_ms(int((t + dt.timedelta(hours=1)).timestamp() * 1000))


class StreamTailer:
    def __init__(self, root: Path, stream: str, start: Key | None = None):
        self.root = Path(root)
        self.stream = stream
        self.start = start
        self.key: Key | None = None
        self.fh = None
        self.partial = b""
        self.bad_lines = 0

    def _open(self, key: Key, path: Path) -> None:
        if self.fh:
            self.fh.close()
        if self.partial:
            log.warning("%s: dropped incomplete final line in %s/%s", self.stream, *self.key)
            self.bad_lines += 1
        self.partial = b""
        self.fh = gzip.open(path, "rb") if path.name.endswith(".gz") else path.open("rb")
        self.key = key

    def _next(self) -> tuple[Key, Path] | None:
        files = stream_files(self.root, self.stream)
        candidates = sorted(k for k in files if (self.key is None and (self.start is None or k >= self.start)) or (self.key is not None and k > self.key))
        return (candidates[0], files[candidates[0]]) if candidates else None

    def _parse(self, chunk: bytes) -> list[dict]:
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                self.bad_lines += 1
        return out

    def poll(self, max_records: int = 200_000) -> list[dict]:
        """All complete records appended since the last call (bounded per call)."""
        records: list[dict] = []
        if self.fh is None:
            nxt = self._next()
            if nxt is None:
                return records
            self._open(*nxt)
        while len(records) < max_records:
            chunk = self.fh.read(1 << 20)
            if chunk:
                records.extend(self._parse(chunk))
                continue
            nxt = self._next()
            if nxt is None:
                break  # caught up with the file the recorder is writing
            self._open(*nxt)  # the recorder has rotated, so the current file is complete
        return records

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None
