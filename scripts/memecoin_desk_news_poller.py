#!/usr/bin/env python3
"""Desk news poller, one-shot (cron every minute, nice 19 / ionice idle). Single instance (flock). Writes <state-dir>/news/<day>.db. Research only."""
import argparse
import fcntl
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_desk_news import Poller  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=str(ROOT / "data" / "desk"))
    a = ap.parse_args()
    Path(a.state_dir, "news").mkdir(parents=True, exist_ok=True)
    lock = open(Path(a.state_dir) / "news" / "poller.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 75
    for r in Poller(a.state_dir).run_once():
        print(json.dumps(r), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
