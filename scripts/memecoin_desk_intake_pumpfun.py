#!/usr/bin/env python3
"""Pump.fun desk intake collector entry (its own script name, so the watchdog's process identity is one feed).

    python3 scripts/memecoin_desk_intake_pumpfun.py      # == memecoin_desk_intake.py --feed pumpfun --loop
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memecoin_desk_intake import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--feed", "pumpfun", "--loop"]))
