#!/usr/bin/env python3
"""4663 fast lane M2 read: the in-runner lane thread (spec 2026-10-01-memecoin-4663-fast-lane-design.md
§2.9; Alpha's go-live gate 2026-10-01). Read-only on data/memecoin_rh_fast_lane.db and the funded
ledger.

Reports, over --since:
  lane         signals, reasons, handoff ms (detector -> runner), busy ms (the lane's whole
               per-signal work, the funded hook's gates included), thread CPU ms (its GIL hold),
               lock-busy share, dropped datagrams. A `hook:<status>:<why>` reason is the funded
               hook's own refusal (the same gates today's 4663 entry passes)
  stats        the safety cache's market-stats hit rate at signal time, and runner_shadow's
               staleness cost: how often one fresh DexScreener read after the decision would have
               changed the hook's verdict (Alpha 2026-10-01; Gamma reads it before live), plus the
               lane's DexScreener budget (calls, 429s; a 429 stops the lane's reads)
  gate         the lane's GIL hold p50/p90 once >= 5 signals have measured it
  overlap      decide->send of funded decisions whose window overlaps a lane busy window, against
               the rest of the same period: the Solana slot lane first, then every other funded
               decision. The stop rule (spec §2.9) concludes only at >= 10 overlapped slot-lane
               sends: overlapped p90 slower than max(1 ms, the non-overlapped p90's bootstrap 95%
               half-width) means STOP.

    python3 scripts/research/rh_fast_lane_read.py --since 2026-10-02T00:00:00+00:00
"""
import argparse
import collections
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def dist(label, xs, fmt="%.2f"):
    if not xs:
        print("  %s: n=0" % label)
        return
    print(("  %s: n=%d p50 " + fmt + " p90 " + fmt + " max " + fmt)
          % (label, len(xs), q(xs, .5), q(xs, .9), max(xs)))


def epoch(iso):
    return datetime.fromisoformat(str(iso)).timestamp()


def p90_halfwidth(xs, reps=1000, seed=7):
    rng = random.Random(seed)
    boots = sorted(q([rng.choice(xs) for _ in xs], .9) for _ in range(reps))
    return (boots[int(.975 * reps) - 1] - boots[int(.025 * reps)]) / 2.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--feed", default=None, help="the feed's decisions (memecoin_rh_feed_shadow.db)")
    args = ap.parse_args(argv)
    from sniper.util.state_paths import state_path
    path = args.db or str(state_path("data", "memecoin_rh_fast_lane.db"))
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    since_epoch = epoch(args.since)
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM rh_fast_lane_events WHERE recv_epoch >= ? ORDER BY id", (since_epoch,))]
    except sqlite3.OperationalError:           # no signal has reached the lane yet
        rows = []
    print("lane events %d since %s" % (len(rows), args.since))
    reasons = collections.Counter(r["reason"] for r in rows)
    print("reasons:", ", ".join("%s %d" % kv for kv in reasons.most_common()) or "none")
    # runner_shadow judges a signal whose float is short anyway (Alpha 2026-10-02) and labels it
    # `float_short+<what followed>`; the live lane would have stopped there. The rest of this read
    # uses what followed.
    short = sum(1 for r in rows if str(r["reason"]).startswith("float_short+"))
    if short:
        print("  float short (live would stop; shadow judged anyway): %d of %d" % (short, len(rows)))
    for r in rows:
        if str(r["reason"]).startswith("float_short+"):
            r["reason"] = r["reason"].split("+", 1)[1]
    dist("handoff_ms (detector -> runner)", [r["handoff_ms"] for r in rows])
    dist("busy_ms (the lane's per-signal work, hook included)", [r["busy_ms"] for r in rows])
    cpu = [r["cpu_ms"] for r in rows if r["cpu_ms"] is not None]
    dist("cpu_ms (lane GIL hold)", cpu)
    if rows:
        print("  lock busy: %d of %d" % (sum(r["lock_busy"] for r in rows), len(rows)))
        # The safety cache's hit rate at signal time (Gamma): a signal that reached the safety step
        # and found a fresh verdict, against no_safety_verdict / safety_stale.
        after_safety = ("built", "lock_busy", "no_mcap", "no_market_stats", "no_source_trade")
        reached = [r for r in rows if r["reason"] in after_safety + ("no_safety_verdict",
                                                                     "safety_stale")
                   or str(r["reason"]).startswith(("unsafe:", "hook:"))]
        hits = [r for r in reached if r["reason"] not in ("no_safety_verdict", "safety_stale")]
        print("  safety cache hit at signal time: %d of %d" % (len(hits), len(reached)))
        judged = [r for r in hits if not str(r["reason"]).startswith("unsafe:")
                  and r["reason"] != "no_mcap"]
        stats_hits = [r for r in judged if r["reason"] != "no_market_stats"]
        print("  market stats hit at signal time: %d of %d" % (len(stats_hits), len(judged)))
        hooked = collections.Counter(
            str(r["reason"]).split(":")[1] if str(r["reason"]).startswith("hook:") else "passed"
            for r in rows if str(r["reason"]).startswith("hook:") or r["reason"] in (
                "built", "lock_busy"))
        print("  funded hook verdicts:", ", ".join("%s %d" % kv for kv in hooked.most_common())
              or "none")
    try:
        drops = db.execute("SELECT reason, sum(n) FROM rh_fast_lane_drop_counts WHERE at >= ? "
                           "GROUP BY 1", (args.since,)).fetchall()
    except sqlite3.OperationalError:          # a release before the counts table (bfce9d72e)
        drops = []
    print("drops:", ", ".join("%s %d" % (r[0], r[1]) for r in drops) or "none")
    try:
        state = db.execute("SELECT * FROM rh_fast_lane_state ORDER BY at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:           # a release before the state row
        state = None
    if state is not None:
        print("lane state at %s: %s" % (state["at"], (
            "GENERATION FAULT LATCHED (%s): the lane is off until a restart" % state["reason"])
            if state["generation_fault"] else "ok"))
    try:
        live = [dict(r) for r in db.execute("SELECT * FROM rh_fast_lane_live WHERE written_at >= ?",
                                            (args.since,))]
        checks = [dict(r) for r in db.execute(
            "SELECT * FROM rh_fast_lane_fill_checks WHERE written_at >= ?", (args.since,))]
    except sqlite3.OperationalError:           # runner_shadow only: no live tables yet
        live, checks = [], []
    try:
        checks_s = [dict(r) for r in db.execute(
            "SELECT * FROM rh_fast_lane_stats_checks WHERE written_at >= ?", (args.since,))]
        dex = db.execute("SELECT * FROM rh_fast_lane_dex WHERE at >= ? ORDER BY at",
                         (args.since,)).fetchall()
    except sqlite3.OperationalError:           # a release before the staleness checks
        checks_s, dex = [], []
    judged = [c for c in checks_s if c["differ"] is not None]
    print("stats staleness: %d checks, %d with a fresh verdict, %d differ%s" % (
        len(checks_s), len(judged), sum(c["differ"] for c in judged),
        " (%.1f%%)" % (100.0 * sum(c["differ"] for c in judged) / len(judged)) if judged else ""))
    dist("  cached stats age s at the decision", [c["cached_age_s"] for c in checks_s
                                                  if c["cached_age_s"] is not None])
    for c in [c for c in judged if c["differ"]][:10]:
        print("  differ %s: cached %s -> fresh %s" % (c["mint"], c["cached_verdict"],
                                                       c["fresh_verdict"]))
    if dex:
        first, last = dict(dex[0]), dict(dex[-1])
        print("dexscreener budget: %d calls, %d 429s in the window%s" % (
            max(0, last["calls"] - first["calls"]), max(0, last["http_429"] - first["http_429"]),
            "; STOPPED (%s)" % last["stopped"] if last["stopped"] else ""))
    if live:
        lr = collections.Counter(r["reason"] for r in live)
        print("live outcomes:", ", ".join("%s %d" % kv for kv in lr.most_common()))
        dist("blocks behind the source (receipt block - source block)",
             [r["blocks_behind"] for r in live if r["blocks_behind"] is not None], "%d")
        dist("decide -> send ms", [r["decide_to_send_ms"] for r in live
                                   if r["decide_to_send_ms"] is not None])
        dist("sequencer reply ms", [r["send_ms"] for r in live if r["send_ms"] is not None])
        dist("wallet lock held ms (send + receipt, capped)", [
            r.get("lock_ms") for r in live if r.get("lock_ms") is not None])
        paths = collections.Counter(r.get("safety_path") for r in live if r.get("safety_path"))
        print("  safety at signal: cache %d, inline %d (dedicated host only)" % (
            paths["cache"], paths["inline"]))
        dist("inline safety ms (cache miss)", [r["safety_ms"] for r in live if r.get(
            "safety_path") == "inline" and r.get("safety_ms") is not None])
        print("  held replies (> 1 block): %d; fill checks %d, disagree %d" % (
            sum(1 for r in live if r["held"]), len(checks),
            sum(1 for c in checks if not c["agree"])))
        from sniper.persistence.db_core import get_readonly_connection
        pilot_ledger = get_readonly_connection(args.ledger or str(state_path("sniper_tracker.db")))
        try:
            pilot_stop_rule(live, checks, args.feed or str(state_path(
                "data", "memecoin_rh_feed_shadow.db")), ledger=pilot_ledger)
        finally:
            pilot_ledger.close()
    print("GATE lane GIL hold: %s" % (
        "measured on %d signals, p50 %.2f / p90 %.2f ms" % (len(cpu), q(cpu, .5), q(cpu, .9))
        if len(cpu) >= 5 else "NOT YET (%d of 5 signals)" % len(cpu)))

    windows = [(r["busy_start_epoch"], r["busy_end_epoch"]) for r in rows]
    from sniper.persistence.db_core import get_readonly_connection
    ledger = get_readonly_connection(args.ledger or str(state_path("sniper_tracker.db")))
    ledger.execute("PRAGMA busy_timeout=2000")
    groups = collections.defaultdict(lambda: ([], []))
    for origin, network, start, sent in ledger.execute(
            "SELECT origin_event_id, network_id, decision_started_at, transaction_submitted_at "
            "FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_completed "
            "WHERE completed_at >= ? AND transaction_submitted_at IS NOT NULL", (args.since,)):
        try:
            a, b = epoch(start), epoch(sent)
        except (TypeError, ValueError):
            continue
        group = "slot_lane" if str(origin or "").startswith("slotlane:") else "funded:%s" % network
        hit = any(a <= end and start_w <= b for start_w, end in windows)
        groups[group][0 if hit else 1].append((b - a) * 1000.0)
    ledger.close()
    print("overlap (decide -> send ms; overlapped vs not, same period):")
    for group in sorted(groups):
        over, rest = groups[group]
        print("  %s: overlapped n=%d p50 %s p90 %s | rest n=%d p50 %s p90 %s" % (
            group, len(over), *("%.1f" % v if v is not None else "-" for v in (q(over, .5), q(over, .9))),
            len(rest), *("%.1f" % v if v is not None else "-" for v in (q(rest, .5), q(rest, .9)))))
    over, rest = groups["slot_lane"]
    if len(over) < 10:
        print("STOP RULE: exposure too rare to measure (%d overlapped slot-lane sends, need 10)"
              % len(over))
    elif len(rest) < 10:
        print("STOP RULE: too few non-overlapped slot-lane sends to compare (%d)" % len(rest))
    else:
        slower = q(over, .9) - q(rest, .9)
        bound = max(1.0, p90_halfwidth(rest))
        print("STOP RULE: overlapped p90 slower by %.2f ms (bound %.2f ms): %s"
              % (slower, bound, "STOP, switch the lane off" if slower > bound else "pass"))
    return 0


E1_FUNDED_MEDIAN_OVERPAY_SRC = 0.090   # today's funded 4663 median over the source (Gamma's rule)


def _wei_per_atomic(sqrt_x96):
    """A native V4 pool's price as wei per coin atomic: native is currency0, so sqrtPriceX96^2 /
    2^192 is coins per wei and the price the lane pays is its inverse (as LaneEvent prices it)."""
    from fractions import Fraction
    return Fraction(1 << 192, int(sqrt_x96) ** 2)


UNKNOWN_AFTER_S = 600.0                # a send still unresolved this long after its row is H3


def pilot_stop_rule(live, checks, feed_path, *, ledger=None, now=None):
    """Gamma's pre-registered pilot stop rule (docs/audits/2026-10-02-rh-fast-lane-pilot-stop-rule.md),
    per SENT lane decision in send order: est/true (H2 outside [0.97, 1.03]); per fill the exact
    fill (paid wei / got atomic from our receipt), overpay_post, overpay_src, the H1 flag and the
    running revert share; H3 from the fill checks and held bookings; E1 at 10 and 20 fills; E2 at
    10 or more sends. H4 needs tx.from reads around (our_block, our_tx_index), listed per fill."""
    from fractions import Fraction
    try:
        feed = sqlite3.connect("file:%s?mode=ro" % feed_path, uri=True)
        feed.row_factory = sqlite3.Row
        decisions = {str(r["tx_hash"]).lower(): dict(r) for r in feed.execute(
            "SELECT tx_hash, est_post_sqrt, true_post_sqrt, src_in_wei, src_out, chase_pct, "
            "premium FROM rh_fast_decisions")}
    except sqlite3.Error:
        print("pilot stop rule: the feed's decisions are unreadable (%s)" % feed_path)
        return
    sent = sorted((r for r in live if r.get("send_reply")), key=lambda r: r["id"])
    print("pilot stop rule (Gamma): %d sent" % len(sent))
    fills, reverts, hard, n_fills, evaluable = [], 0, [], 0, 0
    for n, r in enumerate(sent, 1):
        added = False
        d = decisions.get(str(r["source_tx"]).lower()) or {}
        reverts += r["reason"] == "reverted"
        line = ["  %s" % str(r["source_tx"])[:12], r["reason"]]
        if d.get("est_post_sqrt") and d.get("true_post_sqrt"):
            est_true = (_wei_per_atomic(d["est_post_sqrt"]) / _wei_per_atomic(d["true_post_sqrt"]))
            line.append("est/true %.4f" % float(est_true))
            if not Fraction(97, 100) <= est_true <= Fraction(103, 100):
                hard.append("H2 %s est/true %.4f" % (str(r["source_tx"])[:12], float(est_true)))
        else:
            line.append("est/true unknown")
        if r.get("paid_wei") and r.get("got_atomic"):
            # Gamma LOW 2026-10-02: the feed can give up on the truth (post_checked=2). E1 needs only
            # the source average; H1 is then UNKNOWN, unless the fill is above the source term alone
            # (the cap is the min of both terms, so that is above the cap). Unknown is not a pass.
            fill = Fraction(int(r["paid_wei"]), int(r["got_atomic"]))
            n_fills += 1
            src_avg = (Fraction(int(d["src_in_wei"]), int(d["src_out"]))
                       if int(d.get("src_in_wei") or 0) > 0 and int(d.get("src_out") or 0) > 0
                       else None)
            src_cap = (src_avg * Fraction(str(d["premium"])) * Fraction(1005, 1000)
                       if src_avg is not None and d.get("premium") else None)
            if d.get("true_post_sqrt"):
                true_post = _wei_per_atomic(d["true_post_sqrt"])
                line.append("overpay_post %+.2f%%" % (100 * float(fill / true_post - 1)))
            if src_avg is not None:
                fills.append(float(fill / src_avg - 1))
                added = True
                line.append("overpay_src %+.2f%%" % (100 * fills[-1]))
            if d.get("true_post_sqrt") and src_cap is not None:
                limit = min(true_post * (1 + Fraction(str(d["chase_pct"])) / 100) * Fraction(1005, 1000),
                            src_cap)
                h1 = "BREACH" if fill > limit else "ok"
            elif src_cap is not None and fill > src_cap:
                h1 = "BREACH (source term)"
            else:
                h1 = "UNKNOWN (%s)" % ("no truth" if d else "no decision")
            evaluable += not h1.startswith("UNKNOWN")
            line += ["H1 %s" % h1, "block %s idx %s" % (r.get("our_block"), r.get("our_tx_index"))]
            if h1.startswith("BREACH"):
                hard.append("H1 %s fill above the exact-state cap" % str(r["source_tx"])[:12])
        line.append("reverts %d/%d" % (reverts, n))
        print(" ".join(line))
        if added and len(fills) in (10, 20):
            med = q(fills, .5)
            print("  E1 at %d fills: median overpay_src %+.2f%% vs %+.1f%% (stop at or above): %s" % (
                len(fills), 100 * med, 100 * E1_FUNDED_MEDIAN_OVERPAY_SRC,
                "STOP" if med >= E1_FUNDED_MEDIAN_OVERPAY_SRC else "pass"))
    if n_fills:
        print("  fills with H1 evaluable: %d of %d" % (evaluable, n_fills))
    if n_fills >= 10 and 3 * (n_fills - evaluable) > n_fills:
        print("  H1 unknown on %d of %d fills, over a third: to Alpha" % (n_fills - evaluable, n_fills))
    held = sum(1 for r in live if r["reason"] == "booking_held")
    disagree = sum(1 for c in checks if not c["agree"])
    if held or disagree:
        hard.append("H3 %d held unbooked, %d fill checks disagree" % (held, disagree))
    # Gamma LOW: a submitted_unknown send is not a revert; one whose reservation is still open
    # UNKNOWN_AFTER_S after its row (recovery has had its window) is unaccounted money: H3.
    now = time.time() if now is None else now
    for r in live:
        if r["reason"] != "submitted_unknown" or ledger is None or r.get("pending_id") is None:
            continue
        row = ledger.execute("SELECT status FROM memecoin_pending_entries WHERE id = ?",
                             (r["pending_id"],)).fetchone()
        age = now - (epoch(r["written_at"]) or now)
        if row is not None and row[0] == "reserved" and age >= UNKNOWN_AFTER_S:
            hard.append("H3 %s still submitted_unknown after %.0f s (pending %s reserved)" % (
                str(r["source_tx"])[:12], age, r["pending_id"]))
    if len(sent) >= 10:
        print("  E2: reverts %d of %d sends (stop at 50%%): %s" % (
            reverts, len(sent), "STOP" if reverts * 2 >= len(sent) else "pass"))
    print("  HARD STOPS: %s" % ("; ".join(hard) if hard else "none (H4 needs the block reads)"))


if __name__ == "__main__":
    sys.exit(main())
