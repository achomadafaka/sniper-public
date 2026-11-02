#!/usr/bin/env python3
"""Classify legacy memecoin clocks without rewriting their source timestamps."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _parse_aware(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def classify_legacy_timestamp(stamp, source, offset_seconds):
    stamp_dt = _parse_aware(stamp)
    source_dt = _parse_aware(source)
    if stamp_dt is None or source_dt is None:
        return None
    delta = stamp_dt - source_dt
    if -300 <= delta.total_seconds() <= 3600:
        return "observed_real_utc", stamp_dt.isoformat()
    normalized = stamp_dt - timedelta(seconds=float(offset_seconds))
    if abs(delta) > timedelta(hours=1) and -300 <= (normalized - source_dt).total_seconds() <= 3600:
        return "legacy_host_wall", normalized.isoformat()
    return None


def _classify_table(conn, *, table, key_col, stamp_col, source_col, normalized_col,
                    offset_seconds, apply, join_sql=""):
    query = (
        f"SELECT t.{key_col} row_key, t.{stamp_col} stamp, {source_col} source, "
        f"t.{normalized_col} normalized, t.clock_domain domain FROM {table} t {join_sql} "
        f"WHERE t.{stamp_col} IS NOT NULL AND {source_col} IS NOT NULL"
    )
    report = {"rows": 0, "classifiable": 0, "updated": 0, "unknown": 0,
              "already_classified": 0}
    for row in conn.execute(query):
        report["rows"] += 1
        if row["domain"] != "legacy_unknown" or row["normalized"] is not None:
            report["already_classified"] += 1
            continue
        classified = classify_legacy_timestamp(row["stamp"], row["source"], offset_seconds)
        if classified is None:
            report["unknown"] += 1
            continue
        report["classifiable"] += 1
        if apply:
            domain, normalized = classified
            cursor = conn.execute(
                f"UPDATE {table} SET {normalized_col}=?, clock_domain=? "
                f"WHERE {key_col}=? AND clock_domain='legacy_unknown' "
                f"AND {normalized_col} IS NULL",
                (normalized, domain, row["row_key"]),
            )
            report["updated"] += cursor.rowcount
    return report


def classify_legacy_clock_rows(conn, *, offset_seconds, apply=False):
    """Classify provable legacy rows; ambiguous rows and original timestamps are untouched."""
    plans = [
        dict(table="memecoin_events", key_col="id", stamp_col="received_at",
             source_col="t.created_at", normalized_col="received_at_normalized"),
        dict(table="memecoin_firehose", key_col="id", stamp_col="received_at",
             source_col="t.created_at", normalized_col="received_at_normalized"),
        dict(table="memecoin_theses", key_col="id", stamp_col="received_at",
             source_col="t.created_at", normalized_col="received_at_normalized"),
        dict(table="memecoin_signal_outcomes", key_col="fomo_event_id", stamp_col="buy_ts",
             source_col="e.created_at", normalized_col="buy_ts_normalized",
             join_sql="JOIN memecoin_events e ON e.fomo_event_id=t.fomo_event_id"),
    ]
    report = {}
    for plan in plans:
        name = f"{plan['table']}.{plan['stamp_col']}"
        report[name] = _classify_table(
            conn, offset_seconds=offset_seconds, apply=apply, **plan,
        )
    if apply:
        conn.commit()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write classifications; default is a read-only dry run")
    args = parser.parse_args()
    from sniper.util.envfiles import load_env_files
    load_env_files(_ROOT)   # not the CWD: a relative path silently reads no .env
    from sniper.persistence.db_core import get_connection
    from sniper.util.clock import offset_s

    offset = offset_s()
    if not offset or abs(offset) < 3600:
        raise SystemExit(f"learned offset is {offset:.0f}s; refusing an unproven classification")
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    report = classify_legacy_clock_rows(conn, offset_seconds=offset, apply=args.apply)
    print(f"{'APPLIED' if args.apply else 'DRY-RUN'} with offset {offset:.0f}s")
    for name, counts in report.items():
        print(f"  {name}: " + " ".join(f"{key}={value}" for key, value in counts.items()))


if __name__ == "__main__":
    main()
