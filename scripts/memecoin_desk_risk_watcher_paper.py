#!/usr/bin/env python3
"""The desk RISK watcher in --paper mode under its own script name, so its watchdog's /proc identity never
matches the live watcher (scripts/lib/desk_proc_match.sh compares argv[1]'s basename).

    python3 scripts/memecoin_desk_risk_watcher_paper.py      # == memecoin_desk_risk_watcher.py --loop --paper
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memecoin_desk_risk_watcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--loop", "--paper"]))
