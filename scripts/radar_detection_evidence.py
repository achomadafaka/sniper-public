#!/usr/bin/env python3
"""Grade what the Opportunity Radar DETECTS, separately from what the paper lane BOOKS.

The 2026-08-11 finding `radar-v6-cohort-has-no-measured-edge` read the paper lane
(`op_paper_outcomes`). Before the paper sampler shipped (77c6af71, live
2026-08-11T18:38:59Z) that lane gave a position a MEDIAN OF ONE executable exit
observation against a declared 5,000 ms cadence, and 103 of 291 got none. A
take-profit / stop / time-exit policy that looks once is not that policy.

This script separates the two questions:

  1. DETECTION -- does an arm reach the target more often than its control? Read from
     `op_insight_alerts`, which the outcome tracker writes independently of the paper
     lane.
  2. LANE HEALTH -- can the paper lane evaluate an exit policy at all?

THE GATE IS THE MATCHED COMPARISON. A pooled arm-against-all-controls difference is a
diagnostic, never a verdict: the arm fires on launches that are not comparable to the
average routable launch. This script reports both and labels which one decides. It
reuses `insights._match_v4_controls` at `insights.GATE_MATCH_SCALE`, so it cannot drift
from the preregistered bands.

Three guards, each of which changed a headline number when it was added:

  * `score_status='SCORED'` only. `DATA_GAP_INVALID` rows carry an outcome field that
    the evaluator already declared untrustworthy (12 v5, 4 v6, 339 control).
  * a null `terminal_return_bps` drops the row from that statistic. It is not a zero.
  * the pooled test permutes CREATOR CLUSTERS, and the matched test permutes the SIGN of
    each matched pair inside its creator cluster. A creator that appears on both sides
    is one cluster, not two independent ones.

Leakage guard: `mfe_bps` is net of `quote_drag_bps`, so a cheap route scores higher for
identical token behavior. The default label adds the drag back (`--label gross`).

Usage:
  python3 scripts/radar_detection_evidence.py
  python3 scripts/radar_detection_evidence.py --arm pump-confirmation-truth-v5 \
      --since 2026-07-30T15:32:00Z
"""
import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.research.memecoin_opportunity.insights import (
    GATE_MATCH_SCALE,
    SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S,
    _match_v4_controls,
)


def _features(raw):
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _cluster_means(rows, label):
    groups = defaultdict(list)
    for row in rows:
        groups[row["creator_id"] or row["alert_id"]].append(label(row))
    return [statistics.mean(values) for values in groups.values()]


def _pooled_test(arm, control, label, *, trials, rng):
    """Creator-clustered two-sided permutation. Diagnostic only, never the gate."""
    arm_means = _cluster_means(arm, label)
    control_means = _cluster_means(control, label)
    if not arm_means or not control_means:
        return None
    observed = statistics.mean(arm_means) - statistics.mean(control_means)
    pool = arm_means + control_means
    size = len(arm_means)
    hits = 0
    for _ in range(trials):
        rng.shuffle(pool)
        if abs(statistics.mean(pool[:size]) - statistics.mean(pool[size:])) >= abs(observed):
            hits += 1
    return (
        statistics.mean(arm_means),
        statistics.mean(control_means),
        observed,
        (hits + 1) / (trials + 1),
    )


def _paired_test(pairs, label, *, trials, rng):
    """Sign-flip randomization on matched pairs, clustered by the arm's creator.

    A creator can own several matched pairs. Flipping each pair independently would
    treat those as independent evidence, so the sign flips per creator cluster.
    """
    clusters = defaultdict(list)
    for arm_row, control_row in pairs:
        clusters[arm_row["creator_id"] or arm_row["alert_id"]].append(
            label(arm_row) - label(control_row)
        )
    deltas = [statistics.mean(values) for values in clusters.values()]
    if not deltas:
        return None
    observed = statistics.mean(deltas)
    hits = 0
    for _ in range(trials):
        flipped = statistics.mean(
            value if rng.random() < 0.5 else -value for value in deltas
        )
        if abs(flipped) >= abs(observed):
            hits += 1
    return observed, (hits + 1) / (trials + 1), len(pairs), len(deltas)


def _load(conn, args):
    arms = args.arm.split(",")
    names = arms + [args.control]
    rows = []
    for row in conn.execute(
        "SELECT alert_id,launch_id,rule_version,creator_id,observed_at,score_status,"
        "mfe_bps,terminal_return_bps,feature_json FROM op_insight_alerts "
        f"WHERE evaluator_version=? AND rule_version IN ({','.join('?' * len(names))}) "
        "AND venue_id=? AND observed_at>=? AND score_status='SCORED'",
        (args.evaluator, *names, args.venue, args.since),
    ):
        record = dict(row)
        record["features"] = _features(record.pop("feature_json"))
        rows.append(record)
    return arms, rows


def detection(conn, args, rng):
    arms, rows = _load(conn, args)
    if not rows:
        print("  no SCORED alerts in window")
        return
    arm = [r for r in rows if r["rule_version"] in arms]
    control = [r for r in rows if r["rule_version"] == args.control]

    def hit(row):
        value = row["mfe_bps"]
        if value is None:
            return None
        if args.label == "gross":
            value -= row["features"].get("quote_drag_bps") or 0
        return 1.0 if value >= args.target_bps else 0.0

    def terminal(row):
        value = row["terminal_return_bps"]
        return None if value is None else float(value)

    print(f"  window {args.since} .. now | label={args.label} target={args.target_bps} bps")
    print(
        f"  arm={','.join(arms)}: {len(arm)} SCORED alerts / "
        f"{len({r['creator_id'] for r in arm})} creators | "
        f"control={args.control}: {len(control)} / "
        f"{len({r['creator_id'] for r in control})} creators"
    )

    print("\n  -- pooled, unmatched (DIAGNOSTIC, not the gate) --")
    for name, fn in (("P(MFE>=target)", hit), ("mean terminal_bps", terminal)):
        a = [r for r in arm if fn(r) is not None]
        c = [r for r in control if fn(r) is not None]
        result = _pooled_test(a, c, fn, trials=args.trials, rng=rng)
        if result is None:
            continue
        arm_mean, control_mean, delta, p = result
        print(
            f"    {name:20s} arm={arm_mean:9.3f} control={control_mean:9.3f} "
            f"diff={delta:+9.3f} p={p:.4f}"
        )

    pairs, _unused = _match_v4_controls(arm, control, scale=GATE_MATCH_SCALE)
    print(
        f"\n  -- matched at scale={GATE_MATCH_SCALE} (THE PREREGISTERED GATE) -- "
        f"{len(pairs)} pairs"
    )
    if not pairs:
        print("    no comparable controls; the gate cannot read this arm yet")
        return
    for name, fn in (("P(MFE>=target)", hit), ("mean terminal_bps", terminal)):
        usable = [p for p in pairs if fn(p[0]) is not None and fn(p[1]) is not None]
        result = _paired_test(usable, fn, trials=args.trials, rng=rng)
        if result is None:
            continue
        delta, p, n_pairs, n_clusters = result
        print(
            f"    {name:20s} arm-minus-control={delta:+9.3f} p={p:.4f} "
            f"({n_pairs} pairs / {n_clusters} creator clusters)"
        )

    slow = [
        r for r in arm
        if (r["features"].get("eligible_capital_velocity_atomic_per_s") is not None)
        and r["features"]["eligible_capital_velocity_atomic_per_s"]
        <= SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S
    ]
    fast = [
        r for r in arm
        if (r["features"].get("eligible_capital_velocity_atomic_per_s") is not None)
        and r["features"]["eligible_capital_velocity_atomic_per_s"]
        > SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S
    ]
    if slow and fast:
        result = _pooled_test(slow, fast, hit, trials=args.trials, rng=rng)
        arm_mean, control_mean, delta, p = result
        print(
            f"\n  -- anti-blitz LABELLED SPLIT (not a gate) -- "
            f"slow={arm_mean:.3f} (n={len(slow)})  fast={control_mean:.3f} (n={len(fast)})"
            f"  diff={delta:+.3f} p={p:.4f}"
        )
        print(
            f"     retention if gated: {100 * len(slow) / (len(slow) + len(fast)):.0f}% "
            f"of alerts. The fast side needs 25 alerts before it can be read."
        )


def lane_health(conn, args):
    """Observations per position and the round-trip cost floor at the first look."""
    observations = defaultdict(list)
    for row in conn.execute(
        "SELECT request_id,scheduled_age_ms,return_bps FROM op_paper_exit_observations "
        "WHERE lifecycle_version=? AND status='executable' ORDER BY scheduled_age_ms",
        (args.lifecycle,),
    ):
        observations[row["request_id"]].append((row["scheduled_age_ms"], row["return_bps"]))
    requests = list(
        conn.execute("SELECT request_id,venue_id,decision_at FROM op_paper_fill_requests")
    )
    for label, predicate in (
        ("before sampler", lambda at: at < args.sampler_at),
        ("since sampler", lambda at: at >= args.sampler_at),
    ):
        by_venue = defaultdict(list)
        for row in requests:
            if predicate(row["decision_at"]):
                by_venue[row["venue_id"]].append(len(observations.get(row["request_id"], ())))
        print(f"  {label}:")
        for venue, counts in sorted(by_venue.items()):
            print(
                f"    {venue:20s} positions={len(counts):4d} "
                f"observations/position median={statistics.median(counts):4.1f} "
                f"zero={counts.count(0)}"
            )
    venues = {row["request_id"]: row["venue_id"] for row in requests}
    floors = defaultdict(list)
    for request_id, series in observations.items():
        if series and request_id in venues:
            floors[venues[request_id]].append(series[0][1])
    print("  round-trip cost floor (first executable observation, bps):")
    for venue, values in sorted(floors.items()):
        values.sort()
        pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]  # noqa: E731
        print(
            f"    {venue:20s} n={len(values):4d} p10={pick(.10):7d} "
            f"p25={pick(.25):7d} median={pick(.50):7d} p75={pick(.75):7d}"
        )
    print("\n  outcomes by lifecycle:")
    for row in conn.execute(
        "SELECT lifecycle_version,status,COUNT(*) n FROM op_paper_outcomes "
        "GROUP BY 1,2 ORDER BY 1,2"
    ):
        print(f"    {row['lifecycle_version']:22s} {row['status']:9s} {row['n']:4d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hot-db", default="data/memecoin_opportunity.db")
    ap.add_argument("--insight-db", default="data/memecoin_opportunity_insights.db")
    ap.add_argument("--arm", default="pump-confirmation-truth-v6")
    ap.add_argument("--control", default="route-control-v2")
    ap.add_argument("--venue", default="pumpfun_solana")
    ap.add_argument("--evaluator", default="alert-outcome-v4")
    ap.add_argument("--lifecycle", default="radar-realistic-v2")
    ap.add_argument("--since", default="2026-08-07T10:00:00Z", help="v6 cutover")
    ap.add_argument("--sampler-at", default="2026-08-11T18:38:59Z")
    ap.add_argument("--target-bps", type=int, default=1_200)
    ap.add_argument("--label", choices=("gross", "net"), default="gross")
    ap.add_argument("--trials", type=int, default=4_000)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    insight = sqlite3.connect(f"file:{args.insight_db}?mode=ro", uri=True)
    insight.row_factory = sqlite3.Row
    hot = sqlite3.connect(f"file:{args.hot_db}?mode=ro", uri=True)
    hot.row_factory = sqlite3.Row

    print("== DETECTION (alert outcome tracker, independent of the paper lane)")
    detection(insight, args, rng)
    print("\n== PAPER LANE HEALTH (can it evaluate an exit policy at all?)")
    lane_health(hot, args)
    print("\n  A stop below the cost floor fires on admission drag, not on the token.")


if __name__ == "__main__":
    main()
