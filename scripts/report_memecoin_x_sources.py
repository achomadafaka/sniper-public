#!/usr/bin/env python3
"""Read-only 24h/7d/30d report for the memecoin X source graph."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.analysis.memecoin_x.read_models import build_source_graph
from sniper.persistence.db_core import get_readonly_connection


def _open_report_connection(db_path):
    return get_readonly_connection(str(Path(db_path).expanduser()))


def report_from_path(db_path, *, as_of=None, offset_path=None):
    if as_of is None:
        as_of, health = x_observed_at_or_none(path=offset_path)
        if as_of is None:
            raise ValueError(
                f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    conn = _open_report_connection(db_path)
    try:
        return build_source_graph(conn, as_of=as_of)
    finally:
        conn.close()


def _print_text(report):
    print(f"as_of={report['as_of']}")
    for label, window in report["windows"].items():
        coverage = window["coverage"]
        print(
            f"{label} sources={len(window['sources'])} "
            f"suggestions={len(window['suggestions'])} "
            f"official={len(window['official_context'])} "
            f"buy_coverage={coverage['linked_tier1_buys']}/"
            f"{coverage['eligible_tier1_buys']} "
            f"gaps={coverage['coverage_gap_runs']}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--as-of", help="aware ISO cutoff; defaults to corrected current UTC")
    parser.add_argument(
        "--clock-offset-file",
        help="trusted fresh clock-offset snapshot used when --as-of is omitted",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = report_from_path(
            args.db, as_of=args.as_of, offset_path=args.clock_offset_file)
    except (OSError, sqlite3.Error, ValueError) as exc:
        detail = " ".join(str(exc).split())[:240]
        print(f"source graph unavailable: {detail}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
