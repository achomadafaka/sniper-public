#!/usr/bin/env python3
"""Read-only first-origin re-entry cohort anchored to a causally known REAL close.

This is a coverage diagnostic, not a funded gate replay or executable PnL estimate. The paper
outcome uses the existing funded_clone model. Run with an explicit chain and frozen UTC window:

    python3 scripts/research/refusal_reentry_cohort.py --network-id solana \
      --since 2026-09-25T00:00:00+00:00 --until 2026-09-26T18:15:00+00:00
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.persistence.db_memecoin import measurable_outcome_sql  # noqa: E402


def utc(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None


def build_report(conn, *, network_id, since, until):
    start, end = utc(since), utc(until)
    if not start or not end or end <= start:
        raise ValueError("since and until must be ordered, timezone-aware ISO timestamps")
    if not network_id:
        raise ValueError("network_id is required")

    # Date-only bounds keep the SQL scan bounded without relying on SQLite's text ordering of
    # differently formatted ISO timestamps. The exact instant filter below uses aware datetimes.
    lower = start.date().isoformat()
    upper = (end.date() + dt.timedelta(days=1)).isoformat()
    attempts = conn.execute(
        "SELECT attempt_id, origin_event_id, mint, decision_started_at, completed_at, "
        "terminal_status, terminal_reason FROM memecoin_entry_attempts "
        "WHERE network_id=? AND decision_started_at>=? AND decision_started_at<?",
        (network_id, lower, upper),
    ).fetchall()
    real_closes = defaultdict(list)
    invalid_real_closes = 0
    for row in conn.execute(
        "SELECT id, mint, closed_at, closed_recorded_at FROM memecoin_positions "
        "WHERE network_id=? AND execution_mode='real' AND is_open=0",
        (network_id,),
    ):
        closed = utc(row["closed_at"])
        if closed is None:
            invalid_real_closes += 1
            continue
        real_closes[row["mint"]].append((closed, row["id"], utc(row["closed_recorded_at"])))
    for closes in real_closes.values():
        closes.sort(key=lambda item: (item[0], item[1]))

    counts = {"not_approved_attempts": 0, "clock_eligible_origins": 0,
              "first_coin_origins": 0,
              "late_close_censored": 0, "late_attempt_censored": 0,
              "missing_origin": 0, "invalid_real_closes": invalid_real_closes,
              "paper_missing": 0, "paper_open": 0, "paper_pending": 0,
              "paper_unpriced": 0, "paper_priced": 0, "paper_censored": 0, "paper_ambiguous": 0}
    eligible = {}
    for attempt in attempts:
        if (attempt["terminal_status"] != "refused"
                or not str(attempt["terminal_reason"] or "").startswith("not_approved")):
            continue
        decision = utc(attempt["decision_started_at"])
        if decision is None:
            raise ValueError(f"invalid decision_started_at on {attempt['attempt_id']}")
        if not start <= decision < end:
            continue
        completed = utc(attempt["completed_at"])
        if completed is None or completed >= end:
            counts["late_attempt_censored"] += 1
            continue
        counts["not_approved_attempts"] += 1
        closes = real_closes.get(attempt["mint"], ())
        prior = next((item for item in reversed(closes) if item[0] < decision), None)
        if prior is None:
            continue
        closed, position_id, recorded = prior
        if recorded is None or recorded >= decision:
            counts["late_close_censored"] += 1
            continue
        delay_s = (decision - closed).total_seconds()
        if not 300 <= delay_s <= 7200:
            continue
        origin = attempt["origin_event_id"]
        if not origin:
            counts["missing_origin"] += 1
        identity = (attempt["mint"], origin or f"missing:{attempt['attempt_id']}")
        row = {"network_id": network_id, "mint": attempt["mint"],
               "attempt_id": attempt["attempt_id"], "origin_event_id": origin,
               "decision_started_at": decision.isoformat(), "prior_real_position_id": position_id,
               "prior_real_closed_at": closed.isoformat(), "prior_close_delay_s": delay_s}
        current = eligible.get(identity)
        if current is None or (row["decision_started_at"], row["attempt_id"]) < (
                current["decision_started_at"], current["attempt_id"]):
            eligible[identity] = row
    counts["clock_eligible_origins"] = len(eligible)

    # Choose the first eligible origin on each coin BEFORE joining a paper outcome. A later winner
    # cannot stand in for a first origin that the paper lane failed to open.
    first = {}
    for row in eligible.values():
        coin = row["mint"]
        current = first.get(coin)
        if current is None or (row["decision_started_at"], row["attempt_id"]) < (
                current["decision_started_at"], current["attempt_id"]):
            first[coin] = row
    rows = sorted(first.values(), key=lambda row: (row["mint"], row["decision_started_at"]))
    counts["first_coin_origins"] = len(rows)

    paper = defaultdict(list)
    for position in conn.execute(
        "SELECT id, mint, origin_event_id, is_open, opened_at, opened_recorded_at, closed_at, "
        "closed_recorded_at, realized_pnl, "
        f"({measurable_outcome_sql()}) AS measurable FROM memecoin_positions "
        "WHERE network_id=? AND strategy='funded_clone' AND execution_mode='paper'",
        (network_id,),
    ):
        opened, recorded = utc(position["opened_at"]), utc(position["opened_recorded_at"])
        if (opened is not None and opened <= end and recorded is not None and recorded <= end
                and position["origin_event_id"]):
            paper[(position["mint"], position["origin_event_id"])].append(position)

    pnl = []
    for row in rows:
        matches = paper.get((row["mint"], row["origin_event_id"]), ()) if row["origin_event_id"] else ()
        row["paper_position_id"] = None
        row["paper_pnl_usd"] = None
        if not matches:
            status = "missing"
        elif len(matches) != 1:
            status = "ambiguous"
        else:
            position = matches[0]
            row["paper_position_id"] = position["id"]
            closed, recorded = utc(position["closed_at"]), utc(position["closed_recorded_at"])
            if position["is_open"] == 1:
                status = "open"
            elif closed is None or recorded is None or closed > end or recorded > end:
                status = "pending"
            elif position["realized_pnl"] is None:
                status = "unpriced"
            elif not position["measurable"]:
                status = "censored"
            elif not math.isfinite(float(position["realized_pnl"])):
                status = "unpriced"
            else:
                status = "priced"
                row["paper_pnl_usd"] = float(position["realized_pnl"])
                pnl.append(row["paper_pnl_usd"])
        row["paper_status"] = status
        counts[f"paper_{status}"] += 1

    # The preregistered re-entry read has a 30-close floor. Counts remain visible below it so
    # coverage can be audited, but neither individual nor aggregate PnL is an early result.
    readable = len(pnl) >= 30
    if not readable:
        for row in rows:
            row["paper_pnl_usd"] = None
    return {"report_version": 2, "network_id": network_id, "since": start.isoformat(), "until": end.isoformat(),
            "attempt_family": "not_approved", "paper_arm": "funded_clone",
            "anchor_basis": "latest_real_close_known_at_decision",
            "gate_basis": "not_replayed", "pnl_basis": "funded_clone_paper_model",
            "prior_close_window_s": [300, 7200], "counts": counts,
            "read_status": "diagnostic_paper_model" if readable else "not_estimable_n_lt_30",
            "paper_pnl_median_usd": statistics.median(pnl) if readable else None,
            "paper_pnl_sum_usd": sum(pnl) if readable else None, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="funded ledger path; defaults to DB_PATH or sniper_tracker.db")
    parser.add_argument("--network-id", required=True)
    parser.add_argument("--since", required=True, help="inclusive UTC instant")
    parser.add_argument("--until", required=True, help="exclusive UTC instant and outcome cutoff")
    args = parser.parse_args()
    conn = get_readonly_connection(args.db)
    try:
        report = build_report(conn, network_id=args.network_id, since=args.since, until=args.until)
    finally:
        conn.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('build_report', 'main'))

if __name__ == "__main__":
    raise SystemExit(main())
