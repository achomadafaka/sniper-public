#!/usr/bin/env python3
"""Entry feature capture, the phase-1 coverage report (spec 2026-09-29, 48 h after the P1 deploy).

Read-only on `data/memecoin_entry_features.db` and the funded ledger. For every chain and lane:
rows, tiers and outcomes; per feature, the share that is 'ok' and the reasons it is not; the
recomputed consensus against the runner's own number where the runner computed one (review LOW 5);
and the capture lag (computed_at - completed_at), because the tape keeps only an hour.

    python3 scripts/research/entry_capture_coverage.py [--since 2026-09-30T00:00:00+00:00]
"""
import argparse
import collections
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FEATURES = ("src_prior_n", "src_size_usd", "src_size_over_median", "src_prior_buys_mint",
            "consensus_n_60m", "consensus_n_60m_runner", "chase_display_gap_pct",
            "chase_quote_gap_pct", "source_drift_pct", "flow_30s", "flow_60s", "flow_300s",
            "pre_source_60s", "copier_queue", "same_block_buyers_after_source", "stored_facts",
            "top1_share", "top10_share", "sol_real_open_at_decision")


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.util.state_paths import state_path
    path = args.db or str(state_path("data", "memecoin_entry_features.db"))
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM entry_features WHERE decision_at >= ? ORDER BY decision_at",
                      (args.since,)).fetchall()
    if not rows:
        print("no rows")
        return 1
    print("rows %d, %s .. %s" % (len(rows), rows[0]["decision_at"][:19], rows[-1]["decision_at"][:19]))
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["network_id"], r["signal_source"] or "?")].append(r)
    for (chain, lane), rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        tiers = collections.Counter(r["tier"] for r in rs)
        fills = sum(r["outcome"] == "filled" for r in rs)
        print("\n== %s / %s: %d rows, fills %d, tiers %s" % (chain, lane, len(rs), fills, dict(tiers)))
        cov = collections.defaultdict(collections.Counter)
        agree = collections.Counter()
        for r in rs:
            coverage, features = json.loads(r["coverage_json"]), json.loads(r["features_json"])
            for name in FEATURES:
                cov[name][coverage.get(name, "absent")] += 1
            runner, ours = features.get("consensus_n_60m_runner"), features.get("consensus_n_60m")
            if runner is not None and ours is not None:
                agree["equal" if float(runner) == float(ours) else "differ"] += 1
        for name in FEATURES:
            c = cov[name]
            ok = c.get("ok", 0)
            rest = ", ".join("%s %d" % (k, v) for k, v in c.most_common() if k != "ok")[:120]
            print("   %-32s ok %5.1f%%  %s" % (name, 100.0 * ok / len(rs), rest))
        if agree:
            print("   consensus recompute vs runner: %s" % dict(agree))
    t1_report(rows)
    lags = [lag for lag in (_ts(r["computed_at"]) - _ts(r["completed_at"])
                            if _ts(r["computed_at"]) and _ts(r["completed_at"]) else None
                            for r in rows) if lag is not None]
    if lags:
        lags.sort()
        print("\ncapture lag computed_at - completed_at: p50 %.1f s  p90 %.1f s  max %.1f s"
              % (statistics.median(lags), lags[int(0.9 * (len(lags) - 1))], lags[-1]))
    return 0


def t1_report(rows):
    """P2 (Gamma MEDIUM on 752569c54): the Solana T1 rows' flow_30s and top10_share state (ok,
    pending, expired, other), split by the book at the decision (flat vs busy), so the covered and
    expired rows can be compared. A busy-heavy expired share means the covered sample leans quiet."""
    t1 = [r for r in rows if r["network_id"] == "solana" and (r["tier"] or 0) >= 1]
    if not t1:
        return
    print("\nT1 Solana rows: %d" % len(t1))
    for name in ("flow_30s", "top10_share"):
        split = collections.defaultdict(collections.Counter)
        for r in t1:
            coverage, features = json.loads(r["coverage_json"]), json.loads(r["features_json"])
            state = coverage.get(name, "absent")
            kind = ("ok" if state == "ok" else "pending" if state == "pending:t1" else
                    "expired" if state == "unavailable:t1_expired" else
                    # R5 (2026-10-07): a window censored for an operation no swap explains.
                    "unproved" if state == "unavailable:unproved_operation" else
                    "off" if state == "not_attempted:t1" else
                    "absent" if state == "absent" else "other")
            n_open = features.get("sol_real_open_at_decision")
            book = "unknown" if n_open is None else ("flat" if n_open == 0 else "busy")
            split[book][kind] += 1
        for book in ("flat", "busy", "unknown"):
            c = split.get(book)
            if c:
                total = sum(c.values())
                print("   %-12s book %-7s n=%-5d %s" % (name, book, total, ", ".join(
                    "%s %d (%.0f%%)" % (k, v, 100.0 * v / total) for k, v in c.most_common())))
    # Holders are a state read taken when the book is flat, so every fill (tier 2) is read after
    # our exit (Gamma MEDIUM on 1847dc536). A holder study pre-registers a lag cap and compares
    # rows inside one lag bucket and tier only.
    split = collections.defaultdict(collections.Counter)
    for r in t1:
        state = json.loads(r["coverage_json"]).get("top10_share", "absent")
        lag = json.loads(r["read_lag_json"] or "{}").get("top10_share")
        bucket = ("none" if lag is None else "<=60s" if lag <= 60 else "<=1h" if lag <= 3600
                  else ">1h")
        kind = ("ok" if state == "ok" else "pending" if state == "pending:t1" else
                "off" if state == "not_attempted:t1" else "absent" if state == "absent" else
                state)
        split[(r["tier"], bucket)][kind] += 1
    for (tier, bucket), c in sorted(split.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        print("   top10_share  tier %d lag %-7s %s" % (tier, bucket, ", ".join(
            "%s %d" % kv for kv in c.most_common())))


if __name__ == "__main__":
    sys.exit(main())
