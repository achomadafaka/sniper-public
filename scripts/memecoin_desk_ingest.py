#!/usr/bin/env python3
"""Bounded JSONL ingestion into an explicitly provisioned desk file. Never dispatches entries."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence import db_memecoin_desk as store
from sniper.services.memecoin_desk import ingest_candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desk-db", required=True)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--now-iso", required=True)
    parser.add_argument("--input", default="-")
    parser.add_argument("--max-rows", type=int, default=1000)
    args = parser.parse_args(argv)
    if not 1 <= args.max_rows <= 10000:
        parser.error("--max-rows must be 1..10000")
    conn = store.connect(args.desk_db)
    stream = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    failures = 0
    try:
        for ordinal in range(1, args.max_rows + 2):
            line = stream.readline(262145)
            if not line:
                break
            if ordinal > args.max_rows:
                print(json.dumps({"line": ordinal, "error": "row_limit"}))
                return 1
            if len(line) > 262144:
                print(json.dumps({"line": ordinal, "error": "line_limit"}))
                return 1
            try:
                candidate = json.loads(line)
                result = ingest_candidate(conn, candidate, cohort_id=args.cohort_id, now_iso=args.now_iso)
                print(json.dumps({"line": ordinal, "ticket_id": result["ticket_id"],
                                  "is_primary": result["is_primary"], "reason": result["reason"]}))
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                failures += 1
                reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "malformed_json"
                print(json.dumps({"line": ordinal, "error": reason}))
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
        conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
