"""Run the recorders and the board as background processes that outlive VS Code and terminal tabs.

    python -m tape.ctl start            # start everything that isn't running
    python -m tape.ctl status           # running?, uptime, last data written, last log line
    python -m tape.ctl stop             # stop everything (or name some: stop odds board-web)
    python -m tape.ctl restart board    # stop + start (picks up code changes)
    python -m tape.ctl logs kalshi-live # follow one service's log

Services: kalshi-live, kalshi-backup, odds (recorders); board (indexer -> data/board.db) and
board-web (dashboard at http://127.0.0.1:8765).

Each recorder runs in its own session (detached from the terminal that started it), writes its
PID to data/run/<name>.pid and its output to data/logs/<name>.log. While kalshi-live runs,
`caffeinate` holds the Mac awake on power. Closing the lid still sleeps the Mac.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, data_dir

PYTHON = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYTHON).exists():
    PYTHON = sys.executable

# $30 plan (20K credits/month): every 2 min in the 3 h before kickoff, every 30 min otherwise.
# NFL at 3 credits/call uses ~10K/month. Set ODDS_PACING = [] for the default 60 s / 15 min schedule.
ODDS_PACING = ["--slow", "1800", "--fast", "120"]

RECORDERS: dict[str, dict] = {
    "kalshi-live": {"module": "tape.recorder_ws", "args": ["--league", "nfl", "--days", "6"], "stream": "kalshi_ws"},
    "kalshi-backup": {"module": "tape.recorder_rest", "args": ["--league", "nfl", "--days", "6"], "stream": "kalshi_rest"},
    "odds": {"module": "tape.odds_poller", "args": ["--league", "nfl", *ODDS_PACING], "stream": "odds"},
}
BOARD: dict[str, dict] = {
    "board": {"module": "board.indexer", "args": [], "stream": None},
    "board-web": {"module": "board.server", "args": [], "stream": None, "url": "http://127.0.0.1:8765"},
}
SERVICES: dict[str, dict] = {**RECORDERS, **BOARD}
AWAKE = "caffeinate"


def run_dir() -> Path:
    path = data_dir() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(name: str) -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{name}.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(name: str) -> int | None:
    pidfile = run_dir() / f"{name}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if _alive(pid) else None


def stray_pids(module: str, known: int | None) -> list[int]:
    """Same recorder started some other way (e.g. by hand in a terminal tab)."""
    # "--" is required: without it pgrep reads the leading "-m" of the pattern as an option and fails.
    out = subprocess.run(["pgrep", "-f", "--", f"-m {module}( |$)"], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) not in (known, os.getpid())]


def start(names: list[str]) -> int:
    problems = 0
    for name in names:
        spec = SERVICES[name]
        pid = read_pid(name)
        if pid:
            print(f"{name:14s} already running (pid {pid})")
            continue
        strays = stray_pids(spec["module"], None)
        if strays:
            print(f"{name:14s} NOT started: already running outside the controller (pid {', '.join(map(str, strays))}). "
                  f"Stop that one first (Ctrl+C in its tab, or kill {strays[0]}).")
            problems += 1
            continue
        log = log_path(name).open("a")
        log.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} start {name} =====\n")
        log.flush()
        proc = subprocess.Popen(
            [PYTHON, "-m", spec["module"], *spec["args"]],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the terminal / VS Code that launched it
        )
        (run_dir() / f"{name}.pid").write_text(str(proc.pid))
        print(f"{name:14s} started (pid {proc.pid}), log: {log_path(name)}")
        if name == "kalshi-live":
            awake = subprocess.Popen([AWAKE, "-is", "-w", str(proc.pid)], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (run_dir() / "caffeinate.pid").write_text(str(awake.pid))
    time.sleep(3)
    for name in names:  # catch immediate crashes (missing key, bad config)
        if not read_pid(name) and (run_dir() / f"{name}.pid").exists():
            print(f"{name:14s} EXITED right after start. Last log lines:")
            print("   " + "\n   ".join(_tail(log_path(name), 5)))
            problems += 1
    return 1 if problems else 0


def stop(names: list[str]) -> int:
    for name in names:
        pid = read_pid(name)
        if not pid:
            print(f"{name:14s} not running")
            continue
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
        (run_dir() / f"{name}.pid").unlink(missing_ok=True)
        print(f"{name:14s} stopped (pid {pid})")
    return 0


def _tail(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        return [l for l in lines if l.strip()][-n:]
    except FileNotFoundError:
        return []


def _last_write_age(stream: str) -> float | None:
    newest = None
    for f in (data_dir() / "raw" / stream).glob("*/*"):
        m = f.stat().st_mtime
        newest = m if newest is None or m > newest else newest
    return None if newest is None else time.time() - newest


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def status(_: list[str]) -> int:
    down = 0
    for name, spec in SERVICES.items():
        pid = read_pid(name)
        strays = stray_pids(spec["module"], pid)
        if pid:
            started = dt.datetime.fromtimestamp((run_dir() / f"{name}.pid").stat().st_mtime)
            state = f"RUNNING  pid {pid}, up {_ago((dt.datetime.now() - started).total_seconds()).replace(' ago', '')}"
        elif strays:
            state = f"RUNNING  outside controller (pid {', '.join(map(str, strays))})"
        else:
            state = "STOPPED"
            down += 1
        detail = f"data {_ago(_last_write_age(spec['stream']))}" if spec.get("stream") else spec.get("url", "")
        print(f"{name:14s} {state:44s} {detail}")
        last = _tail(log_path(name), 1)
        if last:
            print(f"{'':14s} {last[0][:110]}")
    return 1 if down else 0


def logs(names: list[str]) -> int:
    path = log_path(names[0])
    if not path.exists():
        print(f"no log yet at {path}")
        return 1
    os.execvp("tail", ["tail", "-n", "40", "-f", str(path)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs"])
    parser.add_argument("names", nargs="*", choices=[[], *SERVICES], help="default: everything")
    args = parser.parse_args()
    names = args.names or list(SERVICES)
    if args.command == "logs" and len(names) != 1:
        parser.error("logs takes exactly one name: " + ", ".join(SERVICES))
    if args.command == "restart":
        stop(names)
        sys.exit(start(names))
    sys.exit({"start": start, "stop": stop, "status": status, "logs": logs}[args.command](names))


if __name__ == "__main__":
    main()
