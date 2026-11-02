#!/usr/bin/env python3
"""4663 fast lane: was a `state_stale` refusal's pre-state right? (Alpha P1, 2026-10-14)

The detector refuses a signal whose pool state on the tape is more than
MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS older than the source's block, even with the tape current
(the SHROOM rule: an unrelated old pool must not stand in for the source swap). This grades the
refusals that never reached would_send, read-only, from what was stored at decision time:

  gradable   the feed's pre-state is the decision's pool and block (rh_feed_signals.pre_*), and the
             source's real swap is known in that pool (rh_fast_decisions.true_*: enrich_true)
  correct    the source's own pool input, swapped through that pre-state with the decide's own math
             (swap_native_in / swap_one_for_zero), lands on the source's real post sqrt price
  not_reproduced  it does not: a stale pre-state OR a source swap that crossed a tick (that math holds
             one liquidity), so it bounds the stale share from above (Gamma LOW 2)
  caps       the funded slow path's verdict on the same source tx (memecoin_entry_attempts), the
             nearest measurement of the market the lane would have faced: filled, or refused by its
             chase / premium caps, or something else

The tape keeps about an hour, so an older refusal whose stored pre-state is another pool or block is
counted as not gradable, never guessed.

    python3 scripts/research/rh_state_stale_grade.py --since 2026-10-07T00:00:00+00:00
"""
import argparse
import collections
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REL_TOL = 1e-6          # the decide's math reproduces real 4663 tape swaps exactly (its tests)


def grade_row(row):
    """'correct', 'wrong:<rel diff>', or 'not_gradable:<why>' for one joined refusal row."""
    from sniper.services.memecoin_rh_fast_decide import swap_native_in, swap_one_for_zero
    if row["pre_pool"] != row["pool"] or row["pre_block"] != row["state_block"]:
        return "not_gradable:pre_state_not_stored"
    if row["true_checked"] != 1 or row["true_pool"] != row["pool"]:
        return "not_gradable:source_swap_not_in_pool"
    try:
        pre_sqrt, liquidity = int(row["pre_sqrt_price_x96"]), int(row["pre_liquidity"])
        true_post, a0, out1 = int(row["true_post_sqrt"]), int(row["true_in_wei"]), int(row["true_out"])
    except (TypeError, ValueError):
        return "not_gradable:malformed"
    if a0 > 0 and out1 > 0:                         # currency0 in (amount0 > 0, amount1 < 0)
        got = swap_native_in(pre_sqrt, liquidity, a0)
    elif a0 < 0 and out1 < 0:                       # currency1 in (amount1 = -out1 > 0)
        got = swap_one_for_zero(pre_sqrt, liquidity, -out1)
    else:
        return "not_gradable:source_swap_shape"
    if got is None:
        return "not_gradable:empty_pool"
    rel = abs(got[0] - true_post) / true_post
    return "correct" if rel <= REL_TOL else "not_reproduced:%.2e" % rel


def slow_path(ledger, row):
    """The funded slow path's terminal verdict on the same source buy, bucketed: the on-chain lane's
    attempt on the tx, else an exact retained J7 source tx by the same wallet and mint in the
    bounded window. UUID-only candidates are censored, never treated as the same source.

    THE WINDOW IS BUILT IN PYTHON, IN THE STORED FORMAT (Gamma BLOCK on 10cad397f): the attempts keep
    ISO 'T' timestamps, and SQLite datetime() writes a space, which sorts below 'T', so a datetime()
    window never matched a same-day row. And the J7 attempt must be the SAME wallet's."""
    from datetime import datetime, timedelta
    from sniper.persistence.db_memecoin import source_tx_of
    tx, mint = row["tx_hash"], row["mint"]
    got = ledger.execute(
        "SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts WHERE network_id='4663' "
        "AND mint=? AND origin_event_id=?", (mint, "onchain:4663:%s:%s" % (tx, mint))).fetchone()
    if got is None and row.get("decided_at") and row.get("source_user_id"):
        at = datetime.fromisoformat(str(row["decided_at"]).replace("Z", "+00:00"))
        candidates = ledger.execute(
            "SELECT terminal_status, terminal_reason, origin_event_id FROM memecoin_entry_attempts WHERE network_id='4663' "
            "AND mint=? AND source_user_id=? AND delivery_lane LIKE 'j7%' "
            "AND decision_started_at BETWEEN ? AND ? ORDER BY decision_started_at",
            (mint, row["source_user_id"], (at - timedelta(seconds=5)).isoformat(),
             (at + timedelta(seconds=120)).isoformat()))
        unknown = False
        for candidate in candidates:
            retained = source_tx_of(candidate[2])
            if retained and retained.lower() == str(tx).lower():
                got = candidate[:2]
                break
            unknown |= retained is None
        if got is None and unknown:
            return "source_identity_unknown"
    if got is None:
        return "no_attempt"
    status, reason = got[0] or "", str(got[1] or "")
    if status == "filled":
        return "filled"
    if "post-trade price" in reason or "chase" in reason or "copy price" in reason:
        return "refused_chase"
    if "premium" in reason:
        return "refused_premium"
    return "other:" + status


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args(argv)
    from sniper.util.state_paths import state_path
    db = sqlite3.connect("file:%s?mode=ro" % (args.db or state_path("data", "memecoin_rh_feed_shadow.db")),
                         uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "WITH st AS (SELECT tx_hash, MIN(id) id FROM rh_fast_decisions WHERE decided_at >= :s "
        "AND reason='state_stale' AND tx_hash NOT IN (SELECT tx_hash FROM rh_fast_decisions "
        "WHERE decided_at >= :s AND reason='would_send') GROUP BY tx_hash) "
        "SELECT d.*, f.pre_pool, f.pre_block, f.pre_sqrt_price_x96, f.pre_liquidity, f.source_user_id "
        "FROM st JOIN rh_fast_decisions d ON d.id = st.id "
        "LEFT JOIN rh_feed_signals f ON f.tx_hash = d.tx_hash AND f.signal = 1", {"s": args.since})]
    ledger = sqlite3.connect("file:%s?mode=ro" % (args.ledger or state_path("sniper_tracker.db")), uri=True)
    grades = collections.Counter()
    caps = collections.Counter()
    for row in rows:
        grade = grade_row(row)
        grades[grade.split(":")[0] if grade.startswith("not_reproduced") else grade] += 1
        if grade == "correct":
            caps[slow_path(ledger, row)] += 1
        if grade.startswith("not_reproduced"):
            print("  not_reproduced %s age=%s lag=%s %s" % (row["tx_hash"][:12], row["state_age_blocks"],
                                                          row["tape_lag_blocks"], grade))
    print("state_stale buys never sent since %s: %d" % (args.since, len(rows)))
    for grade, n in grades.most_common():
        print("  %-40s %d" % (grade, n))
    print("correct pre-state rows, the slow path on the same source tx:")
    for verdict, n in caps.most_common():
        print("  %-40s %d" % (verdict, n))


if __name__ == "__main__":
    main()
