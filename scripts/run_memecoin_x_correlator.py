#!/usr/bin/env python3
"""Run one corrected-clock, read-only-source memecoin X correlation pass."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables
from sniper.services.memecoin_x_correlator import run_once


def _open_readonly(path):
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _open_x(path):
    conn = sqlite3.connect(Path(path).expanduser(), timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    return conn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--x-db", required=True)
    parser.add_argument("--fomo-db", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--clock-offset-file", default=str(ROOT / "data" / "clock_offset.json"))
    args = parser.parse_args()
    observed_at, health = x_observed_at_or_none(path=args.clock_offset_file)
    if observed_at is None:
        parser.error(f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    x_conn = _open_x(args.x_db)
    fomo_conn = _open_readonly(args.fomo_db)
    try:
        result = run_once(
            x_conn, fomo_conn, run_id=f"mcx-corr-{uuid.uuid4().hex}",
            observed_at=observed_at, page_size=args.page_size,
            max_pages=args.max_pages)
    finally:
        fomo_conn.close()
        x_conn.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
