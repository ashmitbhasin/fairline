"""Which tape files a copy may take, and how far the tape is complete.

    python -m tape.manifest     # on the EC2 recorder; prints JSON for infra/fl pull

A copy taken while the recorders are writing can catch a file half-written, and a half-written
file that lands next to the finished one hides it for good (readers prefer the plain .jsonl while
an hour is being compressed). So the machine that writes the tape decides what is finished:

- a .jsonl.gz whose .jsonl is gone is complete: the writer deletes the plain file only after the
  compressed copy is fully written;
- a .jsonl with no .gz, from an hour that ended more than LEFTOVER_MS ago, is a file a stopped
  recorder never compressed; nothing will append to it again, so it is copied as it is;
- every other file is still being written or compressed and stays where it is.

The frontier is the start of the earliest hour still open on either Kalshi stream. Everything
before it is complete, so once copied the laptop's indexer can apply it in receive-time order with
no stream running ahead of another. Odds are left out: the odds file moves to a new hour only at
its next poll, up to 30 minutes late, so its hours land late and are applied when they arrive.
"""
from __future__ import annotations

import calendar
import json
import time
from pathlib import Path

from .config import data_dir

HOUR_MS = 3_600_000
LEFTOVER_MS = 2 * HOUR_MS
FRONTIER_STREAMS = ("kalshi_ws", "kalshi_rest")


def hour_start_ms(path: Path) -> int:
    """Start of the UTC hour a tape file holds: raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl[.gz]."""
    return calendar.timegm(time.strptime(f"{path.parent.name} {path.name[:2]}", "%Y-%m-%d %H")) * 1000


def manifest(root: Path, now_ms: int) -> dict:
    root = Path(root)
    raw = root / "raw"
    files: list[Path] = []
    open_hours: dict[str, list[int]] = {s: [] for s in FRONTIER_STREAMS}
    streams = sorted(p for p in raw.iterdir() if p.is_dir()) if raw.exists() else []
    for stream_dir in streams:
        for path in sorted(stream_dir.glob("*/*.jsonl*")):
            if path.name.endswith(".jsonl.gz"):
                if not path.with_suffix("").exists():
                    files.append(path)
                continue
            if not path.name.endswith(".jsonl"):
                continue
            start = hour_start_ms(path)
            compressing = path.with_name(path.name + ".gz").exists()
            if not compressing and start + HOUR_MS + LEFTOVER_MS <= now_ms:
                files.append(path)
            elif stream_dir.name in open_hours:
                open_hours[stream_dir.name].append(start)
    current = now_ms - now_ms % HOUR_MS
    frontier = min(min(hours) if hours else current for hours in open_hours.values())
    return {
        "now_ms": now_ms,
        "frontier_ms": min(frontier, current),
        "files": [str(p.relative_to(root.parent)) for p in files],
    }


def main() -> None:
    print(json.dumps(manifest(data_dir(), int(time.time() * 1000)), separators=(",", ":")))


if __name__ == "__main__":
    main()
