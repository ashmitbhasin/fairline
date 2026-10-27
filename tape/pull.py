"""Keep this laptop current with the EC2 recorder. Runs as the `pull` service (tape.ctl):

- every minute, fetch the recorders' state (`infra/fl report`) into data/run/ec2-now.json, which the
  tracker's Now section shows, since the laptop can't see the server's processes;
- every 10 minutes, copy the closed tape hours and recorder logs down (`infra/fl pull`), record the
  result in data/run/last-pull.json (ok_ms: the last successful copy, which the tracker measures
  uptime up to) and ask the board to rescan the tape so the tracker's numbers include it.

Intervals use the wall clock, so a laptop that wakes after hours asleep copies straight away, and a
failed copy (no network yet after waking) is retried a minute later.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import ROOT, data_dir

log = logging.getLogger("pull")
FL = str(ROOT / "infra" / "fl")
RECOMPUTE = "http://127.0.0.1:8765/api/tracker/refresh"


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)


def report(run: Path) -> bool:
    proc = subprocess.run([FL, "report"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        log.warning("report failed (exit %d): %s", proc.returncode, (proc.stderr or proc.stdout).strip()[-300:])
        return False
    write_json(run / "ec2-now.json", data)
    return True


def pull(run: Path) -> bool:
    started = time.time()
    proc = subprocess.run([FL, "pull"], cwd=ROOT, capture_output=True, text=True, timeout=1800)
    ok = proc.returncode == 0
    error = None if ok else (proc.stderr or proc.stdout).strip()[-300:]
    now = int(time.time() * 1000)
    previous = {}
    try:
        previous = json.loads((run / "last-pull.json").read_text())
    except (OSError, ValueError):
        pass
    write_json(run / "last-pull.json", {"ts_ms": now, "ok": ok, "ok_ms": now if ok else previous.get("ok_ms"),
                                        "took_s": round(time.time() - started, 1), "error": error})
    if ok:
        log.info("pulled in %.0fs", time.time() - started)
        recompute()
    else:
        log.warning("pull failed (exit %d): %s", proc.returncode, error)
    return ok


def recompute() -> None:
    """Ask the board server to rescan the tape now rather than at its hourly scan."""
    req = urllib.request.Request(RECOMPUTE, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Fairline": "1"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as exc:  # board-web not running: its next start scans anyway
        log.info("board not told to rescan: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report-every", type=float, default=60, help="seconds between state reports")
    parser.add_argument("--pull-every", type=float, default=600, help="seconds between tape copies")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = data_dir() / "run"
    run.mkdir(parents=True, exist_ok=True)
    last_pull = 0.0
    while True:
        try:
            if time.time() - last_pull >= args.pull_every and pull(run):
                last_pull = time.time()
            report(run)
        except subprocess.TimeoutExpired as exc:
            log.warning("timed out: %s", exc.cmd)
        time.sleep(args.report_every)


if __name__ == "__main__":
    main()
