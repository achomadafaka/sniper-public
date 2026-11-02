#!/usr/bin/env python3
"""Run the evidence-locked memecoin candidate exit search."""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("path timestamps must be timezone aware")
    return parsed


def _band(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if value < 1_000_000:
        return "<1M"
    if value < 5_000_000:
        return "1-5M"
    if value < 30_000_000:
        return "5-30M"
    return ">30M"


def load_candidate_paths(conn, candidate_id, *, capture_period_s=30):
    expected_per_path = math.ceil(86400 / float(capture_period_s))
    tolerance_s = max(90.0, 2 * float(capture_period_s))
    paths = []
    observed_total = 0
    positions = [dict(row) for row in conn.execute(
        "SELECT p.*,s.feature_json FROM memecoin_candidate_positions p "
        "JOIN memecoin_candidate_signals s ON s.signal_id=p.signal_id "
        "WHERE p.candidate_id=? AND p.evidence_stage='candidate' "
        "AND p.control_for_signal_id IS NULL ORDER BY p.opened_at,p.position_id",
        (candidate_id,),
    )]
    for position in positions:
        start = _time(position["opened_at"])
        deadline = start + timedelta(hours=24)
        capture_end = deadline + timedelta(seconds=tolerance_s)
        quotes = [dict(row) for row in conn.execute(
            "SELECT id,observed_at,price,quality FROM memecoin_quote_observations_v2 "
            "WHERE capture_version=2 AND network_id=? AND mint=? "
            "AND observed_at>? AND observed_at<=? ORDER BY observed_at,id",
            (position["network_id"], position["mint"], start.isoformat(), capture_end.isoformat()),
        )]
        valid = [row for row in quotes if row["quality"] == "ok" and row["price"] is not None
                 and _time(row["observed_at"]) <= deadline]
        observed_total += len(valid)
        reached_deadline = bool(quotes) and _time(quotes[-1]["observed_at"]) >= deadline
        path_coverage = min(1.0, len(valid) / expected_per_path)
        try:
            features = json.loads(position["feature_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            features = {}
        paths.append({
            "path_id": str(position["position_id"]), "network_id": position["network_id"],
            "mint": position["mint"], "signal_at": position["opened_at"],
            "entry_price": position["entry_price"], "notional": position["entry_notional"],
            "observations": quotes, "complete": reached_deadline and path_coverage >= 0.90,
            "market_cap_band": _band(features.get("market_cap")),
            "coverage": path_coverage,
        })
    expected_total = len(paths) * expected_per_path
    if paths:
        first = min(_time(path["signal_at"]) for path in paths)
        last = max(_time(path["signal_at"]) + timedelta(hours=24) for path in paths)
        budget_skipped = conn.execute(
            "SELECT COALESCE(SUM(budget_skipped_count),0) FROM memecoin_quote_sweeps_v2 "
            "WHERE capture_version=2 AND observed_at>=? AND observed_at<=?",
            (first.isoformat(), last.isoformat()),
        ).fetchone()[0]
    else:
        budget_skipped = 0
    return paths, {
        "expected": expected_total, "observed": observed_total,
        "budget_skipped": int(budget_skipped or 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_id")
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--asof")
    parser.add_argument("--enroll", action="store_true",
                        help="enroll a surviving generated row into the capped paper pool")
    args = parser.parse_args()
    mode = "rw" if args.enroll else "ro"
    conn = sqlite3.connect(f"file:{args.db}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    from sniper.research.memecoin_strategy.exit_search import walk_forward_search
    from sniper.research.memecoin_strategy.store import get_candidate

    candidate = get_candidate(conn, args.candidate_id)
    if candidate is None:
        raise SystemExit("candidate not found")
    paths, coverage = load_candidate_paths(conn, args.candidate_id)
    asof = args.asof or datetime.now(timezone.utc).isoformat()
    result = walk_forward_search(
        paths, candidate, asof, coverage=coverage, conn=conn if args.enroll else None,
    )
    payload = asdict(result)
    if result.spawned_candidate is not None:
        payload["spawned_candidate"] = result.spawned_candidate.__dict__
    print(json.dumps(payload, indent=2, sort_keys=True))



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
