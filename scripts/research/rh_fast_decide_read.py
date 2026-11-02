#!/usr/bin/env python3
"""4663 fast lane M1 read (spec 2026-10-01-memecoin-4663-fast-lane-design.md §6 M1): what the
sign-less decide shadow would have done with each M0 signal. Read-only on the shadow's own SQLite
file and the funded ledger.

Reports, over --since:
  reasons          would_send share (the addressable share) and every named fallback
  decide_ms        in-memory decide, p50/p90/max, and the feed->decide handoff (a queue here; M2's
                   datagram, reserve, sign and write-ahead are not in it)
  estimate         est/true post-trade price on rows whose source swapped in the pool we chose
                   (the M2 gate is the cap ratio p90 <= 1.00); the measured source fee haircut
  candidates       activity rank of the signal's mint (would a top-K safety cache have held it)
  today            the funded path's own attempt on the same 4663 mint within 120 s
  load             research-host calls, errors and 429s; tape lag. STOP on any 429.

    python3 scripts/research/rh_fast_decide_read.py --since 2026-10-02T00:00:00+00:00
"""
import argparse
import collections
import sqlite3
import sys
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def dist(label, xs, fmt="%.3f"):
    if not xs:
        print("  %s: n=0" % label)
        return
    print(("  %s: n=%d p50 " + fmt + " p90 " + fmt + " max " + fmt)
          % (label, len(xs), q(xs, .5), q(xs, .9), max(xs)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args(argv)
    from sniper.util.state_paths import state_path
    path = args.db or str(state_path("data", "memecoin_rh_feed_shadow.db"))
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM rh_fast_decisions WHERE decided_at >= ? ORDER BY id", (args.since,))]
    # An expired exact-source re-ask is not a decision (Gamma LOW-1 on 11830c4a4): decide_ms 0, a
    # ~2 s handoff and no activity rank would skew every distribution below. Counted on its own.
    expired = collections.Counter(r["reason"].split(":", 1)[1] for r in rows
                                  if str(r["reason"] or "").startswith("source_pending_expired:"))
    rows = [r for r in rows if not str(r["reason"] or "").startswith("source_pending_expired:")]
    print("decisions %d since %s" % (len(rows), args.since))
    if expired:
        print("expired exact-source re-asks %d: %s" % (
            sum(expired.values()), ", ".join("%s %d" % kv for kv in expired.most_common())))
    if rows:
        reasons = collections.Counter(r["reason"] for r in rows)
        print("reasons:", ", ".join("%s %d" % kv for kv in reasons.most_common()))
        print("  addressable (would_send) %.1f%%" % (100.0 * reasons["would_send"] / len(rows)))
        dist("decide_ms", [r["decide_ms"] for r in rows if r["decide_ms"] is not None])
        dist("handoff_ms", [r["handoff_ms"] for r in rows if r["handoff_ms"] is not None])
        dist("tape_lag_blocks", [r["tape_lag_blocks"] for r in rows
                                 if r["tape_lag_blocks"] is not None], "%d")
        dist("pool_quiet_blocks", [r["state_age_blocks"] for r in rows
                                   if r["state_age_blocks"] is not None], "%d")
        post, cap, haircut = [], [], []
        where = collections.Counter()
        moved = collections.Counter()
        for r in rows:
            if r["true_checked"] != 1:
                where["source swap not on the tape" if r["true_checked"] == 2 else "pending"] += 1
                continue
            # Gamma (3): a source routed through ANOTHER pool moved no price in ours, yet decide
            # applied its input to our pool's state: a looser cap. Those rows are split out here.
            where["same pool" if r["true_pool"] == r["pool"] else "other pool"] += 1
            if (r["true_pool"] == r["pool"] and r["state_kind"] == "pre" and r["state_liquidity"]
                    and r["true_liquidity"]):
                moved["liquidity moved (LP change or tick cross)" if r["state_liquidity"]
                      != r["true_liquidity"] else "liquidity unchanged"] += 1
            true_in = int(r["true_in_wei"])
            paid = int(r["paid"]) if r["paid"] else 0
            if paid > 0 and r["eth_usd"] and r["true_pool"] == r["pool"]:   # a routed split is partial
                usd_paid = (Fraction(paid, 10 ** 18) * Fraction(str(r["eth_usd"]))
                            if r["payment_token"] == "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
                            else Fraction(paid, 10 ** 6))
                if usd_paid:
                    haircut.append(float(1 - Fraction(true_in, 10 ** 18)
                                         * Fraction(str(r["eth_usd"])) / usd_paid))
            if (r["state_kind"] == "pre" and r["est_post_sqrt"] and r["true_pool"] == r["pool"]):
                true_sqrt, est_sqrt = int(r["true_post_sqrt"]), int(r["est_post_sqrt"])
                ratio = Fraction(true_sqrt ** 2, est_sqrt ** 2)       # est price / true price
                post.append(float(ratio))
                if r["cap_price"] and r["chase_pct"]:
                    today = Fraction(1 << 192, true_sqrt ** 2) * (1 + Fraction(str(r["chase_pct"])) / 100)
                    cap.append(float(Fraction(str(r["cap_price"])) / today))
        print("source swap vs our pool:", ", ".join("%s %d" % kv for kv in where.most_common()))
        print("  same-pool pre rows:", ", ".join("%s %d" % kv for kv in moved.most_common()) or "none")
        print("estimate (source swapped in our pool, state from before its block):")
        dist("est/true post price", post, "%.4f")
        dist("cap ratio (ours / today's)", cap, "%.4f")
        if cap:
            print("  M2 gate cap-ratio p90 <= 1.00: %s" % ("PASS" if q(cap, .9) <= 1.0 else "FAIL"))
        dist("source fee haircut (1 - pool input / paid)", haircut, "%.4f")
        ranks = [r["activity_rank"] for r in rows]
        print("candidates: rank<=10 %d, <=30 %d, <=50 %d, unranked %d of %d" % (
            sum(1 for x in ranks if x and x <= 10), sum(1 for x in ranks if x and x <= 30),
            sum(1 for x in ranks if x and x <= 50), sum(1 for x in ranks if not x), len(ranks)))
        if rows:                                    # today's path, from the funded ledger read-only
            from sniper.persistence.db_core import get_readonly_connection
            ledger = get_readonly_connection(args.ledger or str(state_path("sniper_tracker.db")))
            ledger.execute("PRAGMA busy_timeout=2000")
            today = collections.Counter()
            for r in rows:
                t = datetime.fromisoformat(r["decided_at"])
                hit = ledger.execute(
                    "SELECT terminal_status FROM memecoin_entry_attempts INDEXED BY "
                    "idx_mc_entry_view_completed WHERE completed_at >= ? AND completed_at <= ? "
                    "AND network_id = '4663' AND lower(mint) = ? ORDER BY completed_at LIMIT 1",
                    ((t - timedelta(seconds=120)).isoformat(), (t + timedelta(seconds=120)).isoformat(),
                     r["mint"])).fetchone()
                today[(r["reason"] == "would_send", hit[0] if hit else "no_attempt")] += 1
            ledger.close()
            print("today's path on the same mint within 120 s (would_send?, terminal_status):")
            for (ws, status), n in sorted(today.items(), key=lambda kv: -kv[1]):
                print("  %-5s %-24s %d" % (ws, status, n))
    beats = [dict(r) for r in db.execute(
        "SELECT * FROM rh_fast_heartbeat WHERE at >= ? ORDER BY at", (args.since,))]
    if len(beats) >= 2:
        mins = max((datetime.fromisoformat(beats[-1]["at"]) - datetime.fromisoformat(beats[0]["at"]))
                   .total_seconds() / 60.0, 1e-9)
        sums = collections.Counter()
        for prev, cur in zip(beats, beats[1:]):
            for k in ("rpc_calls", "rpc_errors", "rpc_429"):
                d = cur[k] - prev[k]
                sums[k] += d if d >= 0 else cur[k]          # a restart reset the counters
        calls, errors, limited = sums["rpc_calls"], sums["rpc_errors"], sums["rpc_429"]
        print("load: research calls %.2f/min, errors %d, 429s %d over %.0f min"
              % (calls / mins, errors, limited, mins))
        dist("tape lag s (heartbeat)", [b["tape_lag_s"] for b in beats])
        if limited > 0:
            print("STOP: research-host 429s in the window (spec §6 M1)")
    else:
        print("load: fewer than 2 heartbeats")
    return 0


if __name__ == "__main__":
    sys.exit(main())
