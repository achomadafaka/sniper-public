#!/usr/bin/env python3
"""Re-stamp the corrected cohort id onto paper events that already ran under today's rails.

Until dfd2c321, `paper_cohort_id` hashed each arm's RESOLVED policy. scalp_c/scalp_c2 resolve an
mcap band and real_shadow resolves a per-chain stop, so identity was really
`release x mcap band x chain` — one bucket per coin. Events that ran under byte-identical rails
landed in different cohorts, and the report (which keeps exactly one cohort) discarded all of them.

This corrects the LABEL, never the experiment. An event is re-stamped only when every configured
arm's policy, re-resolved from that row's own entry_mcap and network_id under the CURRENT settings,
is byte-identical to the `assigned_policy_json` the row was opened with. That is proof the row ran
today's rails. `assigned_policy_json` — the record that actually drove each exit — is never touched,
and an event that fails the check keeps its original contract.

    python3 scripts/backfill_memecoin_paper_cohort.py            # dry run, writes nothing
    python3 scripts/backfill_memecoin_paper_cohort.py --apply
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys

import sys as _sys, pathlib as _pathlib   # a bare `python3 scripts/backfill_memecoin_paper_cohort.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.services.memecoin_policy import build_paper_cohort, resolve_paper_arm_policy
from sniper.settings.memecoin import MEMECOIN_ARMS


def _rails_are_current(row) -> bool:
    """Would today's settings resolve the exact policy this row was opened with?"""
    try:
        _policy_id, _version, payload = resolve_paper_arm_policy(
            row["strategy"], entry_mcap=row["entry_mcap"], network_id=row["network_id"],
        )
        return json.loads(payload) == json.loads(row["assigned_policy_json"] or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def plan(conn, arms):
    """(rewrites, reasons) — rewrites is [(event, cohort_id, cohort_json, [position ids])]."""
    armset = set(arms)
    events = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT id, origin_event_id, strategy, network_id, entry_mcap, entry_notional, "
        "       assigned_policy_json, experiment_status, entry_fill_source, paper_cohort_id "
        "FROM memecoin_positions "
        "WHERE execution_mode='paper' AND paper_cohort_id IS NOT NULL "
        "      AND origin_event_id IS NOT NULL"
    ):
        events[row["origin_event_id"]].append(row)

    rewrites, reasons = [], collections.Counter()
    for event, group in sorted(events.items()):
        seen = collections.Counter(row["strategy"] for row in group)
        if set(seen) != armset or any(count != 1 for count in seen.values()):
            reasons["incomplete_roster"] += 1
            continue
        if any(row["experiment_status"] != "eligible" or row["entry_fill_source"] == "fomo"
               for row in group):
            reasons["fomo_or_not_eligible"] += 1
            continue
        if any(not _rails_are_current(row) for row in group):
            reasons["rails_differ_from_today"] += 1
            continue
        # Arms share one bet per event; a split notional is not one comparable cohort.
        notionals = {round(float(row["entry_notional"] or 0.0), 2) for row in group}
        if len(notionals) != 1:
            reasons["split_notional"] += 1
            continue
        cohort_id, cohort_json = build_paper_cohort(arms, notional_usd=notionals.pop())
        if all(row["paper_cohort_id"] == cohort_id for row in group):
            reasons["already_correct"] += 1
            continue
        rewrites.append((event, cohort_id, cohort_json, [row["id"] for row in group]))
    return rewrites, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args()

    arms = tuple(dict.fromkeys(MEMECOIN_ARMS))
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    rewrites, reasons = plan(conn, arms)

    print(f"configured arms ({len(arms)}): {', '.join(arms)}")
    for reason, count in reasons.most_common():
        print(f"  {count:5}  skipped: {reason}")
    positions = sum(len(ids) for _e, _c, _j, ids in rewrites)
    print(f"  {len(rewrites):5}  re-stamp: {positions} positions across "
          f"{len({cid for _e, cid, _j, _i in rewrites})} corrected cohort id(s)")
    if not args.apply:
        print("\ndry run — nothing written. re-run with --apply")
        return 0
    if not rewrites:
        return 0

    with conn:
        for _event, cohort_id, cohort_json, ids in rewrites:
            conn.executemany(
                "UPDATE memecoin_positions SET paper_cohort_id=?, paper_cohort_json=? WHERE id=?",
                [(cohort_id, cohort_json, pid) for pid in ids],
            )
    print(f"applied: {positions} positions re-stamped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
