"""Copy the EC2 recorder's closed tape hours and recorder logs down to this laptop every few minutes,
so the board and the tracker here stay current. Runs as the `pull` service (tape.ctl) and calls
`infra/fl pull`; a sleeping laptop simply catches up on its next run.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import time

from .config import ROOT

log = logging.getLogger("pull")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=float, default=600, help="seconds between pulls")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    while True:
        started = time.time()
        proc = subprocess.run([str(ROOT / "infra" / "fl"), "pull"], cwd=ROOT, capture_output=True, text=True)
        if proc.returncode == 0:
            log.info("pulled in %.0fs", time.time() - started)
        else:
            log.warning("pull failed (exit %d): %s", proc.returncode, (proc.stderr or proc.stdout).strip()[-300:])
        time.sleep(args.every)


if __name__ == "__main__":
    main()
