"""Run the recorders and the board as background processes that outlive VS Code and terminal tabs.

    python -m tape.ctl start            # start everything that isn't running
    python -m tape.ctl status           # running?, uptime, last data written, last log line
    python -m tape.ctl stop             # stop everything (or name some: stop odds board-web)
    python -m tape.ctl restart board    # stop + start (picks up code changes)
    python -m tape.ctl logs kalshi-live # follow one service's log
    python -m tape.ctl run kalshi-live  # run one service in the foreground (what systemd calls on EC2)
    python -m tape.ctl autostart on     # start the board and pull at each Mac login (launchd); off removes it

Services: kalshi-live, kalshi-backup, odds (recorders); board (indexer -> data/board.db) and
board-web (dashboard at http://127.0.0.1:8765).

Each recorder runs in its own session (detached from the terminal that started it), writes its
PID to data/run/<name>.pid and its output to data/logs/<name>.log. While kalshi-live runs,
`caffeinate` holds the Mac awake on power. Closing the lid still sleeps the Mac.

On the EC2 recorder (infra/) systemd runs each service as fairline@<name> and restarts it if it
dies; there start/stop/restart go through systemctl and status reads the same PID files. Once
infra/fl has set up that server, the laptop runs only the board and `pull`, which copies the
server's closed tape hours and recorder logs down every 10 minutes; recorders start here only when
named (a fallback that also spends Odds API credits twice if the server is recording). `autostart on`
installs a launchd agent that starts those three at each login, so a reboot doesn't leave them stopped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import plistlib
import shutil
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
PULL: dict[str, dict] = {"pull": {"module": "tape.pull", "args": [], "stream": None}}
SERVICES: dict[str, dict] = {**RECORDERS, **BOARD, **PULL}
# Written by `infra/fl up` on the laptop only (git-ignored, never copied to the server).
REMOTE_RECORDER = ROOT / "infra" / "ssh_config"
AWAKE = "caffeinate" if sys.platform == "darwin" else None
SYSTEMD_UNIT = Path("/etc/systemd/system/fairline@.service")
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.fairline.autostart.plist"
# Outside the repo: if macOS stops launchd's processes reading ~/Desktop, the error still lands here.
LAUNCH_LOG = Path.home() / "Library" / "Logs" / "fairline-autostart.log"


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


def _runs_module(pid: int, module: str) -> bool:
    """Is this PID still the service? After a reboot a stale pid file's number can belong to anything."""
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return f"-m {module}" in out or f"tape.ctl run" in out


def read_pid(name: str) -> int | None:
    pidfile = run_dir() / f"{name}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if _alive(pid) and _runs_module(pid, SERVICES[name]["module"]) else None


def stray_pids(module: str, known: int | None) -> list[int]:
    """Same recorder started some other way (e.g. by hand in a terminal tab)."""
    # "--" is required: without it pgrep reads the leading "-m" of the pattern as an option and fails.
    out = subprocess.run(["pgrep", "-f", "--", f"-m {module}( |$)"], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) not in (known, os.getpid())]


def start(names: list[str]) -> int:
    problems = 0
    procs: dict[str, subprocess.Popen] = {}
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
        procs[name] = proc
        (run_dir() / f"{name}.pid").write_text(str(proc.pid))
        print(f"{name:14s} started (pid {proc.pid}), log: {log_path(name)}")
        if name == "kalshi-live" and AWAKE:
            awake = subprocess.Popen([AWAKE, "-is", "-w", str(proc.pid)], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (run_dir() / "caffeinate.pid").write_text(str(awake.pid))
    time.sleep(3)
    for name, proc in procs.items():  # catch immediate crashes (missing key, bad config)
        if proc.poll() is not None:  # an exited child is a zombie until reaped, so ask Popen, not kill(0)
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
        # Recorders get SIGINT: they handle it as KeyboardInterrupt and close their tape files, flushing the
        # last buffered records (SIGTERM would end them without unwinding).
        os.kill(pid, signal.SIGINT if name in RECORDERS else signal.SIGTERM)
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


def status(names: list[str]) -> int:
    down = 0
    for name in names:
        spec = SERVICES[name]
        pid = read_pid(name)
        strays = stray_pids(spec["module"], pid)
        if pid:
            started = (run_dir() / f"{name}.pid").stat().st_mtime
            state = f"RUNNING  pid {pid}, up {_ago(time.time() - started).replace(' ago', '')}"
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


def default_names() -> list[str]:
    if REMOTE_RECORDER.exists():
        return [*BOARD, *PULL]
    return [*RECORDERS, *BOARD]


def run(names: list[str]) -> int:
    """Become the service in this process (exec keeps the PID), for a supervisor such as systemd."""
    name = names[0]
    spec = SERVICES[name]
    (run_dir() / f"{name}.pid").write_text(str(os.getpid()))
    print(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} start {name} =====", flush=True)
    os.chdir(ROOT)
    os.execv(PYTHON, [PYTHON, "-m", spec["module"], *spec["args"]])


def systemd(command: str, names: list[str]) -> int:
    units = [f"fairline@{name}" for name in names]
    code = subprocess.run(["sudo", "systemctl", command, *units]).returncode
    time.sleep(3)
    status(names)
    return code


def autostart(switch: str) -> int:
    """Install (on) or remove (off) the launchd agent that starts the board and pull at each login."""
    if sys.platform != "darwin":
        print("autostart is for the Mac; on EC2 systemd starts the services at boot")
        return 1
    label = LAUNCH_AGENT.stem
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)  # fails if not loaded
    if switch == "off":
        LAUNCH_AGENT.unlink(missing_ok=True)
        print(f"autostart off: removed {LAUNCH_AGENT}")
        return 0
    names = [*BOARD, *PULL]  # named, never default_names(): without infra/ssh_config that list has recorders
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [PYTHON, "-m", "tape.ctl", "start", *names],
        "WorkingDirectory": str(ROOT),  # where -m finds tape
        "RunAtLoad": True,
        "StandardOutPath": str(LAUNCH_LOG),
        "StandardErrorPath": str(LAUNCH_LOG),
    }))
    code = subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT)]).returncode
    print(f"autostart on: {LAUNCH_AGENT} runs `tape.ctl start {' '.join(names)}` at each login, log {LAUNCH_LOG}")
    return code


def logs(names: list[str]) -> int:
    path = log_path(names[0])
    if not path.exists():
        print(f"no log yet at {path}")
        return 1
    os.execvp("tail", ["tail", "-n", "40", "-F", str(path)])  # -F: pull replaces recorder logs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs", "run", "autostart"])
    parser.add_argument("names", nargs="*", choices=[[], *SERVICES, "on", "off"],
                        help="default: everything; autostart takes on or off")
    args = parser.parse_args()
    if args.command == "autostart":
        if args.names not in (["on"], ["off"]):
            parser.error("autostart takes on or off")
        sys.exit(autostart(args.names[0]))
    if {"on", "off"} & set(args.names):
        parser.error("on and off go with autostart")
    names = args.names or default_names()
    if args.command in ("logs", "run") and len(names) != 1:
        parser.error(f"{args.command} takes exactly one name: " + ", ".join(SERVICES))
    if args.command == "status" and not args.names and REMOTE_RECORDER.exists():
        print("recorders run on EC2: infra/fl status")
    if args.command in ("start", "stop", "restart") and SYSTEMD_UNIT.exists() and shutil.which("systemctl"):
        sys.exit(systemd(args.command, names))
    if args.command == "restart":
        stop(names)
        sys.exit(start(names))
    sys.exit({"start": start, "stop": stop, "status": status, "logs": logs, "run": run}[args.command](names))


if __name__ == "__main__":
    main()
