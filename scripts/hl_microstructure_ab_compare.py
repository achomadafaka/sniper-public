#!/usr/bin/env python3
"""Read-only per-policy_id A/B compare for the Hyperliquid microstructure forward test."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.hl_microstructure.ab_compare import (  # noqa: E402
    build_ab_compare,
    format_ab_compare_markdown,
)


DEFAULT_DB = "data/hl_microstructure_paper.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Hyperliquid microstructure A/B compare")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of Markdown")
    args = ap.parse_args(argv)

    uri = f"file:{args.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_ab_compare(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_ab_compare_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
