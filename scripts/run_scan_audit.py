#!/usr/bin/env python3
"""Universe recall audit — Step 2 (forward labels) + Step 3 readout scaffold. Read-only over
the perishable scan log; writes only the derived labels table. Reports recall once enough
refreshes have accumulated."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import scan_audit as SA  # noqa: E402
from sniper.hyperlive import scan_log as SL  # noqa: E402
from sniper.hyperlive import universe as U  # noqa: E402

SCAN_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_scan_log.sqlite"
FIT_MIN_MOVERS = 40         # don't promote learned weights until the label set is non-trivial


def _recall(conn, floor: float) -> None:
    rows = conn.execute("""
        SELECT s.selected_bool, l.cost_clearing_150s, s.day_ntl_vlm
        FROM hyperlive_scan_log s
        JOIN hyperlive_scan_labels l ON s.asset = l.asset AND s.scan_ts = l.scan_ts
        WHERE l.cost_clearing_150s IS NOT NULL
    """).fetchall()
    if not rows:
        print("recall: no labeled rows yet — let the scan log accumulate a few refreshes.")
        return
    trad = [r for r in rows if (r[2] or 0) >= floor]            # only the pool we can pick from
    movers = [r for r in trad if r[1] == 1]
    selected = [r for r in trad if r[0] == 1]
    all_movers = [r for r in rows if r[1] == 1]
    if not movers:
        print(f"recall: {len(trad)} tradeable labeled rows, 0 tradeable movers yet — quiet market.")
        return
    recall = sum(1 for r in movers if r[0] == 1) / len(movers)
    precision = (sum(1 for r in selected if r[1] == 1) / len(selected)) if selected else 0.0
    print(f"tradeable labeled rows: {len(trad)} (of {len(rows)} logged)  "
          f"tradeable movers: {len(movers)} (of {len(all_movers)} incl. sub-floor micro-caps)")
    print(f"recall@12  = {recall:.1%}   (TRADEABLE movers that were in the selected top-12)")
    print(f"precision@12 = {precision:.1%}   (selected that were real movers)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Universe recall audit (Steps 2-3)")
    ap.add_argument("--scan-db", default=SCAN_DB)
    ap.add_argument("--no-recall", action="store_true")
    ap.add_argument("--no-fit", action="store_true",
                    help="skip fitting/persisting live weights (shadow-only run)")
    ap.add_argument("--weights-path", default=U.WEIGHTS_PATH)
    ap.add_argument("--min-movers", type=int, default=FIT_MIN_MOVERS)
    args = ap.parse_args(argv)
    if not os.path.exists(args.scan_db):
        print(f"scan log not found at {args.scan_db} — engine hasn't logged yet.")
        return 0
    conn = SL.connect(args.scan_db)
    try:
        labels = SA.build_labels(conn)
        wrote = SA.write_labels(conn, labels)
        refreshes = conn.execute("SELECT COUNT(DISTINCT scan_ts) FROM hyperlive_scan_log").fetchone()[0]
        print(f"scan log: {refreshes} refreshes  |  labeled {wrote} (asset, scan_ts) rows")
        floor = U.MIN_VOLUME_USD
        if not args.no_recall:
            _recall(conn, floor)
            lift = SA.recall_lift(conn, min_volume=floor)
            if lift["status"] == "insufficient":
                print(f"Phase-2 accel-lift: insufficient labeled movers yet (scans={lift['scans']}).")
            else:
                print(f"Phase-2 accel-lift (mark-oracle acceleration): "
                      f"recall@12 {lift['hand_recall_at_12']:.0%} -> {lift['accel_recall_at_12']:.0%} "
                      f"(lift {lift['lift_pp']:+.1f}pp, bar {lift['bar_pp']}pp; "
                      f"precision {lift['hand_precision_at_12']:.0%} -> {lift['accel_precision_at_12']:.0%}) "
                      f"=> {'PROMOTE to live score' if lift['promote'] else 'stay SHADOW'}  (n={lift['movers']} movers)")
            # B — swing-track validation: does the shared top-12 selection catch LONG-horizon
            # movers (30/60 min), or does swing need its own ranking?
            for hz in SA.SWING_HORIZONS_MS:
                sw = SA.recall_at_horizon(conn, hz, min_volume=floor, min_move_bps=SA.SWING_MIN_MOVE_BPS)
                if sw["status"] != "ok":
                    print(f"swing recall @{hz//60000}min: insufficient history yet "
                          f"(scans={sw['scans']}, need rows ~{hz//60000}min apart).")
                else:
                    print(f"swing recall @{sw['horizon_s']//60}min (>{sw['min_move_bps']:.0f}bps, "
                          f"tradeable): recall@12 {sw['recall']:.1%}  precision@12 {sw['precision']:.1%}  "
                          f"(n={sw['movers']} swing movers, {sw['scans']} scans)")
            # C — cross-venue (Binance->HL) basis as a selection signal (accrues forward from
            # the day the basis column started logging).
            cv = SA.cross_venue_lift(conn, min_volume=floor)
            if cv["status"] != "ok":
                print(f"cross-venue basis: accruing — {cv.get('mapped_movers', 0)} mapped tradeable "
                      f"movers so far (need {cv.get('min_movers')}). No verdict yet.")
            else:
                print(f"cross-venue basis lift (held-out: {cv['test_scans']} scans, {cv['test_movers']} movers): "
                      f"hand {cv['hand_recall']:.1%} | basis-alone {cv['basis_recall']:.1%} | "
                      f"hand+basis {cv['combined_recall']:.1%} (lift {cv['lift_pp']:+.1f}pp) => "
                      f"{'PROMOTE basis into score' if cv['promote'] else 'no edge'}")
            # C-directional: does sign(basis) predict HL's next move, net of the ~9bps fee?
            for hz in (150_000, 300_000):
                for thr in (2.0, 10.0):
                    de = SA.basis_direction_edge(conn, horizon_ms=hz, basis_threshold_bps=thr, min_volume=floor)
                    if de["status"] != "ok":
                        print(f"basis-direction @{hz//1000}s |basis|>={thr:.0f}bps: accruing "
                              f"(n={de['n']}/{de['min_n']}).")
                    else:
                        print(f"basis-direction @{de['horizon_s']}s |basis|>={thr:.0f}bps: "
                              f"aligned {de['mean_aligned_bps']:+.1f}bps, after-fee {de['after_fee_bps']:+.1f}bps "
                              f"(hit {de['hit_rate']:.0%}, t={de['t_stat']}, n={de['n']}) => "
                              f"{'EDGE' if de['edge'] else 'no edge'}")
            hr = SA.horse_race(conn, min_volume=floor)
            if hr["status"] != "ok":
                print(f"horse-race: insufficient time blocks yet ({hr}).")
            else:
                print(f"horse-race recall@12 (held-out: {hr['test_scans']} scans, {hr['test_movers']} movers): {hr['recall_at_12']}")
                print(f"  winner: {hr['winner']}   learned weights: "
                      f"{ {k: v for k, v in hr['learned_weights'].items() if v > 0} }")
        if not args.no_fit:
            fit = SA.fit_weights(conn, min_movers=args.min_movers, min_volume=floor)
            if fit["status"] != "ok":
                print(f"live weights NOT updated (status={fit['status']}, "
                      f"movers={fit.get('movers')}/{args.min_movers}) — selection stays on hand weights.")
            else:
                # PROMOTION GATE: learned must beat hand on held-out TRADEABLE data, else fall
                # back to hand (the file is removed → engine load_weights()=None → HAND_WEIGHTS).
                ev = SA.evaluate_weights(conn, fit["weights"], U.HAND_WEIGHTS, min_volume=floor)
                if ev.get("status") == "ok" and ev["promote"]:
                    U.save_weights(fit["weights"], meta={"movers": fit["movers"],
                                   "nonmovers": fit.get("nonmovers"), "separation": fit["separation"],
                                   "heldout_recall_learned": ev["cand_recall"],
                                   "heldout_recall_hand": ev["base_recall"]}, path=args.weights_path)
                    print(f"live weights PROMOTED ({fit['movers']} movers; held-out recall@12 "
                          f"learned {ev['cand_recall']:.1%} > hand {ev['base_recall']:.1%}) -> "
                          f"{ {k: v for k, v in fit['weights'].items() if v} }")
                else:
                    if os.path.exists(args.weights_path):
                        os.remove(args.weights_path)
                    detail = (f"held-out recall@12 learned {ev['cand_recall']:.1%} vs hand "
                              f"{ev['base_recall']:.1%}" if ev.get("status") == "ok"
                              else f"eval {ev.get('status')}")
                    print(f"live weights stay HAND — learned did not clear the +{int(SA.PROMOTE_MARGIN*100)}pp "
                          f"bar ({detail}). Removed {args.weights_path}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
