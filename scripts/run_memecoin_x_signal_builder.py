#!/usr/bin/env python3
"""Run one corrected-clock deterministic memecoin X signal pass."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables
from sniper.services.memecoin_x_signal_builder import run_once


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--clock-offset-file", default=str(ROOT / "data/clock_offset.json"))
    args = parser.parse_args(argv)
    observed_at, health = x_observed_at_or_none(path=args.clock_offset_file)
    if observed_at is None:
        parser.error(f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    conn = sqlite3.connect(Path(args.db).expanduser(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_memecoin_x_tables(conn)
        result = run_once(
            conn, run_id=f"mcx-signal-{uuid.uuid4().hex}",
            observed_at=observed_at, limit=args.limit)
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
