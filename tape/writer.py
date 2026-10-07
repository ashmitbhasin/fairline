"""Append-only JSONL writer, rotated hourly (UTC) and gzipped once an hour is closed.

Layout: <data>/raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl  ->  <HH>.jsonl.gz after rotation.
Plain JSONL while open means a crash loses at most the last unflushed second.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import shutil
import threading
import time
from pathlib import Path


def now_ms() -> int:
    return int(time.time() * 1000)


def _compress(path: Path) -> None:
    gz = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


class JsonlWriter:
    def __init__(self, root: Path, stream: str, flush_every_s: float = 1.0, compress: bool = True):
        self.base = Path(root) / "raw" / stream
        self.flush_every_s = flush_every_s
        self.compress = compress
        self._path: Path | None = None
        self._fh = None
        self._last_flush = 0.0
        self.records = 0

    def path_for(self, ts_ms: int) -> Path:
        t = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
        return self.base / t.strftime("%Y-%m-%d") / f"{t:%H}.jsonl"

    def write(self, record: dict) -> None:
        ts = record.get("recv_ts_ms") or now_ms()
        path = self.path_for(ts)
        if path != self._path:
            self._rotate(path)
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.records += 1
        if time.monotonic() - self._last_flush >= self.flush_every_s:
            self._fh.flush()
            self._last_flush = time.monotonic()

    def _rotate(self, path: Path) -> None:
        old = self._path
        if self._fh:
            self._fh.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._path = path
        if old and self.compress and old.exists():
            threading.Thread(target=_compress, args=(old,), daemon=True).start()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
