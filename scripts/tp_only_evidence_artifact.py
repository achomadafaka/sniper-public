#!/usr/bin/env python3
"""Reproducible artifact for the tp-only-v1 take-profit table. READ-ONLY.

The commit that introduced tp-only-v1 said its parameters were "measured, not chosen". That was
FALSE and this script exists to keep the claim honest: +50 was the argmax of a 21-cell search
(7 take-profit levels x 3 caps) over 12 coins. It is a retrospective candidate, its 90% interval
crosses zero, and nothing here is forward evidence.

What is reproducible, and the reason the level is not arbitrary: the SAME coins fire at every
take-profit from +15 to +50, so a lower target wins no extra trades and takes less from an
identical set. +50 is an interior maximum rather than a corner. That structure is the argument,
not the point estimate.

Unit is the MINT. Marks are bounded by `as_of` on both sides.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import numpy as np

sys.path.insert(0, "/home/bigpapi/Desktop/Sniper")

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
AS_OF = "2026-08-11T14:00:00+00:00"
HORIZONS = (60, 300, 900, 3600, 21600, 86400)
CAPS = {"15min": 2, "1h": 3, "6h": 4}
COST_PCT = 4.5
CELLS_SEARCHED = 21


def paths_for(conn, handle, nets, as_of):
    """One path per MINT: our paper entry price, then the coin's marks as % from that entry."""
    from sniper.persistence import db_memecoin as m
    uid = conn.execute("SELECT source_user_id u FROM memecoin_wallets WHERE handle=?",
                       (handle,)).fetchone()
    if not uid:
        return []
    q = ("SELECT mint, entry_price, opened_at FROM memecoin_positions WHERE source_user_id=? "
         "AND strategy='alpha_b' AND COALESCE(execution_mode,'paper')!='real' AND entry_price>0 "
         "AND opened_at < ? AND " + m.COIN_NETWORK_SQL + " IN (%s) AND "
         % ",".join("?" * len(nets)) + m.measurable_outcome_sql())
    best = {}
    for p in conn.execute(q, (uid["u"], as_of, *nets)):
        if p["mint"] in best and best[p["mint"]][0] <= str(p["opened_at"]):
            continue
        o = conn.execute("SELECT outcome_id FROM memecoin_signal_outcomes_v2 WHERE mint=? "
                         "AND source_user_id=? ORDER BY signal_observed_at LIMIT 1",
                         (p["mint"], uid["u"])).fetchone()
        if not o:
            continue
        hz = {int(r["target_s"]): 100.0 * (float(r["price"]) / float(p["entry_price"]) - 1.0)
              for r in conn.execute(
                  "SELECT target_s, price FROM memecoin_horizon_marks_v2 WHERE outcome_id=? "
                  "AND status='observed' AND price>0 AND observed_at IS NOT NULL "
                  "AND observed_at < ?", (o["outcome_id"], as_of))}
        path = [hz[t] for t in HORIZONS if t in hz]
        if path:
            best[p["mint"]] = (str(p["opened_at"]), path)
    return [v[1] for v in best.values()]


def outcomes(paths, tp, stop, cap_idx):
    out = []
    for p in paths:
        r = p[min(cap_idx, len(p) - 1)]
        for x in p[:cap_idx + 1]:
            if x >= tp:
                r = tp
                break
            if x <= -stop:
                r = -stop
                break
        out.append(r - COST_PCT)
    return np.array(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="tp-only-v1 evidence artifact (read-only)")
    ap.add_argument("--handle", default="randomuser123")
    ap.add_argument("--chain", default="solana")
    ap.add_argument("--as-of", default=AS_OF)
    ap.add_argument("--stop", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=4)
    a = ap.parse_args(argv)
    nets = ("4663", "robin") if a.chain in ("robin", "4663") else (a.chain,)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    paths = paths_for(conn, a.handle, nets, a.as_of)
    if not paths:
        print(f"{a.handle}/{a.chain}: no coins")
        return 1
    rng = np.random.default_rng(a.seed)
    print(f"{a.handle} / {a.chain} · stop -{a.stop:.0f}% · mirror OFF · net {COST_PCT}% cost")
    print(f"as_of={a.as_of}  n={len(paths)} COINS (unit = mint)\n")
    print(f"{'TP':<7}" + "".join(f"cap {k}".ljust(12) for k in CAPS) + "  fires  90% CI")
    best = None
    for tp in (10, 20, 30, 50, 75, 100, 150):
        line = f"+{tp}%".ljust(7)
        for cap in CAPS.values():
            v = outcomes(paths, tp, a.stop, cap)
            line += f"{v.mean():+6.1f}%".ljust(12)
            if best is None or v.mean() > best[0]:
                best = (v.mean(), tp, cap)
        v = outcomes(paths, tp, a.stop, CAPS["6h"])
        bs = v[rng.integers(0, len(v), size=(20000, len(v)))].mean(axis=1)
        fires = sum(1 for p in paths if any(x >= tp for x in p[:CAPS["6h"] + 1]))
        lo, hi = np.percentile(bs, [5, 95])
        print(line + f"  {fires}/{len(paths)}".ljust(8) + f"[{lo:+.1f},{hi:+.1f}]")
    v = outcomes(paths, best[1], a.stop, best[2])
    bs = v[rng.integers(0, len(v), size=(20000, len(v)))].mean(axis=1)
    half = len(v) // 2
    print(f"\nargmax: TP +{best[1]}%  EV {v.mean():+.1f}%/coin  P(>0)={100*(bs>0).mean():.0f}%")
    print(f"  halves {v[:half].mean():+.1f} / {v[half:].mean():+.1f}   "
          f"ex-best coin {(v.sum()-v.max())/(len(v)-1):+.1f}%")
    print(f"\n{CELLS_SEARCHED} cells were searched to find this one. The interval is not corrected "
          f"for that.\nRETROSPECTIVE CANDIDATE — not forward evidence, and not a funding signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
