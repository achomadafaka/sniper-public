#!/usr/bin/env python3
"""Print a read-only Axiom and J7 receipt comparison for an explicit interval."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence.db_core import get_readonly_connection
from sniper.services.axiom_feed_compare import compare_feeds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', help='SQLite ledger path; defaults to the configured state ledger')
    parser.add_argument('--chain')
    parser.add_argument('--since', required=True, help='Inclusive ISO receipt time')
    parser.add_argument('--until', required=True, help='Exclusive ISO receipt time')
    args = parser.parse_args(argv)
    conn = get_readonly_connection(args.db)
    try:
        report = compare_feeds(conn, chain=args.chain, since=args.since, until=args.until)
    finally:
        conn.close()
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
