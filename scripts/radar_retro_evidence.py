#!/usr/bin/env python3
"""Reproduce the 2026-08-07 retro edge claims from the DURABLE insight DB.

Audit finding 6: the +723 bps/alert (retro-v6, TP=1200@60s), the ~-450 bps measured
pair cost, and the derived estimates were recorded in specs without a committed query.
This script IS the query. It reads only `data/memecoin_opportunity_insights.db`
(mode=ro), prints the cohort filters, the per-TP ladder EV for the retro-v6 cohort,
the measured pair-cost medians by launch mode, and a manifest (row counts, window,
sha256 over the sorted cohort alert ids) so a reviewer can pin what the numbers were
computed FROM even after the live DB accrues further.

Scoring contract (matches the 2026-08-07 hunt): ladder cell result 'hit' credits the
TP barrier; 'loss' takes the cell's measured crossing quote (worst_return_bps);
'timeout' takes the alert's terminal_return_bps. Cells: target-ladder-v2 rows only
(the hunt predates v3); horizon 60s.

Usage: python3 scripts/radar_retro_evidence.py [--db PATH] [--since ISO] [--until ISO]
Defaults reproduce the hunt window (2026-07-30T15:32 -> 2026-08-07T06:22).
"""
import argparse
import hashlib
import json
import sqlite3
import zlib


TARGETS = (200, 400, 600, 800, 1_200)
HORIZONS_MS = (5_000, 10_000, 20_000, 60_000, 120_000, 300_000)
HORIZON_MS = 60_000
FULL_GRID = {(target, horizon) for target in TARGETS for horizon in HORIZONS_MS}


def _features(row):
    raw = row["feature_json"]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/memecoin_opportunity_insights.db")
    ap.add_argument("--since", default="2026-07-30T15:32:00Z")
    ap.add_argument("--until", default="2026-08-07T06:22:39Z")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    alerts = [
        dict(row, features=_features(row))
        for row in conn.execute(
            "SELECT * FROM op_insight_alerts WHERE rule_version='pump-confirmation-truth-v5'"
            " AND evaluator_version='alert-outcome-v4' AND score_status='SCORED'"
            " AND observed_at >= ? AND observed_at <= ? ORDER BY observed_at",
            (args.since, args.until),
        )
    ]
    retro_v6 = []
    seen_launches = set()
    for alert in alerts:
        features = alert["features"]
        if features.get("launch_mode") != "standard":
            continue
        if features.get("observed_pair_return_bps") is None:
            continue
        if int(features["observed_pair_return_bps"]) < -500:
            continue
        cells = {
            (int(row["target_bps"]), int(row["horizon_ms"]))
            for row in conn.execute(
                "SELECT target_bps,horizon_ms FROM op_insight_alert_targets"
                " WHERE alert_id=? AND evaluator_version='target-ladder-v2'",
                (alert["alert_id"],),
            )
        }
        if cells != FULL_GRID:
            continue
        launch_id = str(alert["launch_id"])
        if launch_id in seen_launches:
            continue
        seen_launches.add(launch_id)
        retro_v6.append(alert)
    ids = sorted(str(a["alert_id"]) for a in retro_v6)
    manifest = {
        "db": args.db,
        "window": [args.since, args.until],
        "v5_alerts": len(alerts),
        "retro_v6_cohort": len(ids),
        "retro_v6_creators": len({str(a["creator_id"]) for a in retro_v6}),
        "cohort_sha256": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
        "filters": {
            "evaluator_version": "alert-outcome-v4",
            "score_status": "SCORED",
            "target_grid": "target-ladder-v2:30-cells",
            "dedupe": "first-observed-alert-per-launch",
        },
    }

    print("== retro-v6 cohort (v5 AND standard AND pair >= -500) ==")
    print(json.dumps(manifest, indent=2))

    print("\n== TP ladder EV at 60s (target-ladder-v2 cells; hit=barrier,"
          " loss=measured crossing, timeout=terminal) ==")
    for target in TARGETS:
        returns = []
        for a in retro_v6:
            cell = conn.execute(
                "SELECT result, worst_return_bps FROM op_insight_alert_targets"
                " WHERE alert_id=? AND target_bps=? AND horizon_ms=?"
                " AND evaluator_version='target-ladder-v2'",
                (a["alert_id"], target, HORIZON_MS),
            ).fetchone()
            if cell is None:
                continue
            if cell["result"] == "hit":
                returns.append(target)
            elif cell["result"] == "loss":
                if cell["worst_return_bps"] is not None:
                    returns.append(int(cell["worst_return_bps"]))
            elif cell["result"] == "timeout":
                if a["terminal_return_bps"] is not None:
                    returns.append(int(a["terminal_return_bps"]))
        if returns:
            mean = round(sum(returns) / len(returns))
            hit_n = sum(1 for r in returns if r == target)
            print(f"  TP={target:>5}: n={len(returns):>4}  mean={mean:>+6} bps"
                  f"  hit_rate={hit_n / len(returns):.2f}")
        else:
            print(f"  TP={target:>5}: no scored cells")

    print("\n== measured pair cost by launch mode (all rows through the cutoff) ==")
    by_mode = {}
    for row in conn.execute(
        "SELECT rule_version,feature_json FROM op_insight_alerts WHERE observed_at <= ?",
        (args.until,),
    ):
        feats = _features(row)
        mode = feats.get("launch_mode")
        pair = feats.get("observed_pair_return_bps")
        if mode and pair is not None:
            by_mode.setdefault(str(mode), []).append(int(pair))
    for mode, vals in sorted(by_mode.items()):
        vals.sort()
        print(f"  {mode:>9}: n={len(vals):>5}  median={vals[len(vals) // 2]:>+6} bps")
    conn.close()


if __name__ == "__main__":
    main()
