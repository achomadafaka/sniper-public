#!/usr/bin/env python3
"""Emit the denominator and exclusion manifest for memecoin capture v2."""
from __future__ import annotations

import argparse
import json
import sqlite3


VERSIONED_TABLES = {
    "memecoin_feed_observations_v2",
    "memecoin_quote_sweeps_v2",
    "memecoin_quote_observations_v2",
    "memecoin_signal_outcomes_v2",
}


def assert_capture_version(conn, table, expected=2):
    if table not in VERSIONED_TABLES:
        raise ValueError(f"unsupported versioned table: {table}")
    versions = [row[0] for row in conn.execute(
        f"SELECT DISTINCT capture_version FROM {table} ORDER BY capture_version",
    )]
    if any(version != expected for version in versions):
        raise ValueError(
            f"mixed capture versions in {table}: {versions}; expected only {expected}",
        )
    return versions


def replay_feed_rows(conn, capture_version=2):
    assert_capture_version(conn, "memecoin_feed_observations_v2", capture_version)
    return list(conn.execute(
        "SELECT * FROM memecoin_feed_observations_v2 WHERE capture_version=? "
        "ORDER BY observed_at, COALESCE(source_ts,''), upstream_event_id",
        (capture_version,),
    ))


def _counts(conn, sql, params=()):
    return {str(row[0]): int(row[1]) for row in conn.execute(sql, params)}


def build_capture_manifest(conn, capture_version=2):
    if capture_version != 2:
        raise ValueError("capture v2 manifest requires capture_version=2")
    for table in VERSIONED_TABLES:
        assert_capture_version(conn, table, capture_version)

    feed = conn.execute(
        "SELECT COUNT(*) rows, MIN(observed_at) first_observed_at, "
        "MAX(observed_at) last_observed_at, COUNT(DISTINCT raw_type) raw_types "
        "FROM memecoin_feed_observations_v2 WHERE capture_version=?",
        (capture_version,),
    ).fetchone()
    raw_types = _counts(
        conn,
        "SELECT raw_type, COUNT(*) FROM memecoin_feed_observations_v2 "
        "WHERE capture_version=? GROUP BY raw_type ORDER BY raw_type",
        (capture_version,),
    )
    cohort_flags = conn.execute(
        "SELECT SUM(CASE WHEN was_curated IS NOT NULL THEN 1 ELSE 0 END) curated_known, "
        "SUM(CASE WHEN was_real_approved IS NOT NULL THEN 1 ELSE 0 END) real_known, "
        "SUM(CASE WHEN was_curated IS NULL OR was_real_approved IS NULL THEN 1 ELSE 0 END) unknown "
        "FROM memecoin_feed_observations_v2 WHERE capture_version=?",
        (capture_version,),
    ).fetchone()
    chain_coins = conn.execute(
        "SELECT COUNT(*) FROM (SELECT network_id, mint FROM memecoin_feed_observations_v2 "
        "WHERE capture_version=? AND network_id IS NOT NULL AND mint IS NOT NULL "
        "GROUP BY network_id, mint)",
        (capture_version,),
    ).fetchone()[0]
    quote_quality = _counts(
        conn,
        "SELECT quality, COUNT(*) FROM memecoin_quote_observations_v2 "
        "WHERE capture_version=? GROUP BY quality ORDER BY quality",
        (capture_version,),
    )
    excluded = sum(count for quality, count in quote_quality.items() if quality != "ok")
    horizon_counts = _counts(
        conn,
        "SELECT status, COUNT(*) FROM memecoin_horizon_marks_v2 GROUP BY status ORDER BY status",
    )
    horizon_total = sum(horizon_counts.values())
    horizon_observed = horizon_counts.get("observed", 0)
    sweeps = conn.execute(
        "SELECT COUNT(*) sweeps, COALESCE(SUM(eligible_count),0) eligible, "
        "COALESCE(SUM(requested_count),0) requested, COALESCE(SUM(captured_count),0) captured, "
        "COALESCE(SUM(budget_skipped_count),0) budget_skipped "
        "FROM memecoin_quote_sweeps_v2 WHERE capture_version=?",
        (capture_version,),
    ).fetchone()
    cohort_skips = conn.execute(
        "SELECT COALESCE(SUM(skipped_sweep_count),0) FROM memecoin_capture_cohorts_v2",
    ).fetchone()[0]
    outcome_status = _counts(
        conn,
        "SELECT status, COUNT(*) FROM memecoin_signal_outcomes_v2 "
        "WHERE capture_version=? GROUP BY status ORDER BY status",
        (capture_version,),
    )
    networks = [row[0] for row in conn.execute(
        "SELECT DISTINCT network_id FROM memecoin_feed_observations_v2 "
        "WHERE capture_version=? AND network_id IS NOT NULL ORDER BY network_id",
        (capture_version,),
    )]
    quote_span = conn.execute(
        "SELECT COUNT(*) rows, MIN(observed_at) first_observed_at, "
        "MAX(observed_at) last_observed_at FROM memecoin_quote_observations_v2 "
        "WHERE capture_version=?",
        (capture_version,),
    ).fetchone()
    cohort_count = conn.execute("SELECT COUNT(*) FROM memecoin_capture_cohorts_v2").fetchone()[0]
    legacy_excluded = {
        "events": int(conn.execute("SELECT COUNT(*) FROM memecoin_events").fetchone()[0]),
        "firehose": int(conn.execute("SELECT COUNT(*) FROM memecoin_firehose").fetchone()[0]),
        "outcomes": int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_signal_outcomes",
        ).fetchone()[0]),
        "quote_ledger": int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_quote_ledger",
        ).fetchone()[0]),
    }
    return {
        "capture_version": capture_version,
        "feed": {
            "rows": int(feed["rows"]), "first_observed_at": feed["first_observed_at"],
            "last_observed_at": feed["last_observed_at"], "raw_types": int(feed["raw_types"]),
            "raw_type_counts": raw_types,
            "point_in_time_flags": {
                "curated_known": int(cohort_flags["curated_known"] or 0),
                "real_approved_known": int(cohort_flags["real_known"] or 0),
                "unknown_rows": int(cohort_flags["unknown"] or 0),
            },
        },
        "networks": networks,
        "distinct_chain_coins": int(chain_coins),
        "outcomes": outcome_status,
        "quotes": {
            "quality": quote_quality,
            "quality_total": sum(quote_quality.values()),
            "quality_excluded": excluded,
            "rows": int(quote_span["rows"]),
            "first_observed_at": quote_span["first_observed_at"],
            "last_observed_at": quote_span["last_observed_at"],
        },
        "horizons": {
            "total": horizon_total,
            "observed": horizon_observed,
            "missed": horizon_counts.get("missed", 0),
            "abandoned": horizon_counts.get("abandoned", 0),
            "pending": horizon_counts.get("pending", 0),
            "coverage": (horizon_observed / horizon_total if horizon_total else None),
        },
        "sweeps": {
            "count": int(sweeps["sweeps"]), "eligible": int(sweeps["eligible"]),
            "requested": int(sweeps["requested"]), "captured": int(sweeps["captured"]),
            "budget_skipped": int(sweeps["budget_skipped"]),
            "cohort_skip_counter": int(cohort_skips),
            "cohorts": int(cohort_count),
        },
        "legacy_excluded": legacy_excluded,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(json.dumps(build_capture_manifest(conn), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
