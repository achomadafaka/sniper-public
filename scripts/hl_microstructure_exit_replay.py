#!/usr/bin/env python3
"""Run a read-only MFE/MAE exit replay for the Hyperliquid microstructure paper DB."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.hl_microstructure.exit_replay import (  # noqa: E402
    build_exit_replay,
    format_exit_replay_markdown,
)


DEFAULT_DB = "data/hl_microstructure_paper.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Hyperliquid microstructure exit replay")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of Markdown")
    args = ap.parse_args(argv)

    uri = f"file:{args.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_exit_replay(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_exit_replay_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
